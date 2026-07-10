import type { BudgetInput } from "../api/types";

export function BudgetFields({
  value,
  onChange,
}: {
  value: BudgetInput;
  onChange: (value: BudgetInput) => void;
}) {
  const field = (key: keyof BudgetInput, raw: string) => {
    const next = { ...value };
    if (!raw) delete next[key];
    else next[key] = Number(raw);
    onChange(next);
  };
  return (
    <fieldset className="budget-fields">
      <legend>Run budget</legend>
      <label>
        Calls
        <input
          type="number"
          min="0"
          value={value.call_limit ?? ""}
          onChange={(event) => field("call_limit", event.target.value)}
          placeholder="12"
        />
      </label>
      <label>
        Tokens
        <input
          type="number"
          min="0"
          value={value.token_limit ?? ""}
          onChange={(event) => field("token_limit", event.target.value)}
          placeholder="48000"
        />
      </label>
      <label>
        Cost limit <small>µUSD</small>
        <input
          type="number"
          min="0"
          value={value.micro_usd_limit ?? ""}
          onChange={(event) => field("micro_usd_limit", event.target.value)}
          placeholder="250000"
        />
      </label>
      <label>
        Wall time <small>seconds</small>
        <input
          type="number"
          min="0"
          value={value.wall_seconds_limit ?? ""}
          onChange={(event) => field("wall_seconds_limit", event.target.value)}
          placeholder="600"
        />
      </label>
    </fieldset>
  );
}
