import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePolling } from "./usePolling";

function Probe({ task }: { task: () => Promise<string> }) {
  usePolling(task, { intervalMs: 1000 });
  return null;
}

describe("usePolling", () => {
  afterEach(() => vi.useRealTimers());

  it("clears the bounded timer when the view unmounts", async () => {
    const task = vi.fn(async () => "ok");
    const { unmount } = render(<Probe task={task} />);
    await waitFor(() => expect(task).toHaveBeenCalledTimes(1));
    unmount();

    vi.useFakeTimers();
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).toHaveBeenCalledTimes(1);
  });
});
