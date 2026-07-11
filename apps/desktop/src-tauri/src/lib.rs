use reqwest::{redirect::Policy, Client, Method};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{fs, net::Ipv6Addr, path::Path, time::Duration};
use url::{Host, Url};

const MAX_TOKEN_FILE_BYTES: u64 = 16 * 1024;

#[derive(Debug, Deserialize, PartialEq)]
#[serde(rename_all = "UPPERCASE")]
enum RequestMethod {
    Get,
    Post,
    Put,
    Delete,
}

impl RequestMethod {
    fn as_reqwest(&self) -> Method {
        match self {
            Self::Get => Method::GET,
            Self::Post => Method::POST,
            Self::Put => Method::PUT,
            Self::Delete => Method::DELETE,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct DaemonRequest {
    daemon_url: String,
    token_file: String,
    method: RequestMethod,
    path: String,
    body: Option<Value>,
}

#[derive(Debug, Serialize)]
struct DaemonResponse {
    status: u16,
    body: Value,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum ProxyError {
    InvalidTarget,
    InvalidRequest,
    CredentialUnavailable,
    DaemonUnavailable,
    InvalidResponse,
}

fn validate_daemon_url(raw: &str) -> Result<Url, ProxyError> {
    let mut url = Url::parse(raw).map_err(|_| ProxyError::InvalidTarget)?;
    if !matches!(url.scheme(), "http" | "https")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || !matches!(url.path(), "" | "/")
    {
        return Err(ProxyError::InvalidTarget);
    }

    let allowed = match url.host() {
        Some(Host::Domain(host)) => host.eq_ignore_ascii_case("localhost"),
        Some(Host::Ipv4(ip)) => ip.is_loopback() || ip.is_private(),
        Some(Host::Ipv6(ip)) => ip.is_loopback() || is_unique_local(ip),
        None => false,
    };
    if !allowed {
        return Err(ProxyError::InvalidTarget);
    }

    url.set_path("/");
    Ok(url)
}

fn is_unique_local(ip: Ipv6Addr) -> bool {
    ip.segments()[0] & 0xfe00 == 0xfc00
}

fn validate_path(path: &str) -> Result<(), ProxyError> {
    if path.is_empty()
        || path.contains('\\')
        || path.contains('#')
        || path.contains("://")
        || path.starts_with("//")
    {
        return Err(ProxyError::InvalidRequest);
    }
    let route = path.split('?').next().unwrap_or_default();
    let lower = route.to_ascii_lowercase();
    let unsafe_encoding = ["%2e", "%2f", "%5c"]
        .iter()
        .any(|item| lower.contains(item));
    let unsafe_segment = route
        .split('/')
        .any(|segment| matches!(segment, "." | ".."));
    if unsafe_encoding || unsafe_segment || !(route == "/health" || route.starts_with("/v1/")) {
        return Err(ProxyError::InvalidRequest);
    }
    Ok(())
}

fn read_token(token_file: &str) -> Result<String, ProxyError> {
    let path = Path::new(token_file);
    let metadata = fs::metadata(path).map_err(|_| ProxyError::CredentialUnavailable)?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_TOKEN_FILE_BYTES {
        return Err(ProxyError::CredentialUnavailable);
    }
    let token = fs::read_to_string(path).map_err(|_| ProxyError::CredentialUnavailable)?;
    let token = token.trim();
    if token.is_empty() || token.lines().count() != 1 {
        return Err(ProxyError::CredentialUnavailable);
    }
    Ok(token.to_owned())
}

fn build_daemon_client() -> Result<Client, reqwest::Error> {
    Client::builder()
        .no_proxy()
        .redirect(Policy::none())
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(15))
        .build()
}

#[tauri::command]
async fn daemon_request(request: DaemonRequest) -> Result<DaemonResponse, ProxyError> {
    let base = validate_daemon_url(&request.daemon_url)?;
    validate_path(&request.path)?;
    if request.method == RequestMethod::Get && request.body.is_some() {
        return Err(ProxyError::InvalidRequest);
    }

    let token = read_token(&request.token_file)?;
    let target = base
        .join(request.path.trim_start_matches('/'))
        .map_err(|_| ProxyError::InvalidRequest)?;
    let client = build_daemon_client().map_err(|_| ProxyError::DaemonUnavailable)?;

    let mut outbound = client
        .request(request.method.as_reqwest(), target)
        .bearer_auth(token)
        .header(reqwest::header::ACCEPT, "application/json");
    if let Some(body) = request.body {
        outbound = outbound.json(&body);
    }

    let response = outbound
        .send()
        .await
        .map_err(|_| ProxyError::DaemonUnavailable)?;
    let status = response.status().as_u16();
    let bytes = response
        .bytes()
        .await
        .map_err(|_| ProxyError::InvalidResponse)?;
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).map_err(|_| ProxyError::InvalidResponse)?
    };
    Ok(DaemonResponse { status, body })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![daemon_request])
        .run(tauri::generate_context!())
        .expect("error while running Polaris Agent");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_loopback_and_private_urls() {
        for url in [
            "http://127.0.0.1:8765",
            "https://localhost:8765",
            "http://10.0.0.8",
            "http://192.168.1.4:8765",
            "http://[::1]:8765",
            "https://[fd00::1]",
        ] {
            assert!(validate_daemon_url(url).is_ok(), "{url}");
        }
    }

    #[test]
    fn rejects_public_or_ambiguous_urls() {
        for url in [
            "https://example.com",
            "ftp://127.0.0.1",
            "http://user:pass@127.0.0.1",
            "http://127.0.0.1/base",
            "http://127.0.0.1?redirect=1",
        ] {
            assert!(validate_daemon_url(url).is_err(), "{url}");
        }
    }

    #[test]
    fn accepts_only_health_and_versioned_paths() {
        for path in ["/health", "/v1/runs", "/v1/runs/id/timeline?after_id=2"] {
            assert!(validate_path(path).is_ok(), "{path}");
        }
        for path in [
            "/metrics",
            "//example.com/v1/runs",
            "/v1/../health",
            "/v1/%2e%2e/health",
            "https://example.com/v1/runs",
        ] {
            assert!(validate_path(path).is_err(), "{path}");
        }
    }

    #[test]
    fn method_guard_is_narrow() {
        assert_eq!(
            serde_json::from_str::<RequestMethod>("\"GET\"").unwrap(),
            RequestMethod::Get
        );
        assert_eq!(
            serde_json::from_str::<RequestMethod>("\"POST\"").unwrap(),
            RequestMethod::Post
        );
        assert_eq!(
            serde_json::from_str::<RequestMethod>("\"PUT\"").unwrap(),
            RequestMethod::Put
        );
        assert_eq!(
            serde_json::from_str::<RequestMethod>("\"DELETE\"").unwrap(),
            RequestMethod::Delete
        );
    }

    #[test]
    fn daemon_client_disables_environment_proxies() {
        assert!(build_daemon_client().is_ok());
        let no_proxy_call = [".no_", "proxy()"].concat();
        assert!(
            include_str!("lib.rs").contains(&no_proxy_call),
            "daemon client must not forward bearer credentials through environment proxies"
        );
    }
}
