"""End-to-end screener pipeline.

    download -> universe -> layer2 -> signals -> allocator

Every step saves its result to a file and the next step reads that file (not memory), so any step
can also be run on its own. Per-step status lives in data/status/<layer>_status.json and the
overall run in data/status/pipeline_status.json.

Usage (from src/):
    python -m bhav_screener.pipeline                  # whole pipeline
    python -m bhav_screener.pipeline --from layer2    # restart at a step
    python -m bhav_screener.pipeline --only signals   # run one step
"""
import argparse
import time
from typing import Callable

from bhav_screener.bhavcopy_backfill import BhavcopyBackfill
from bhav_screener.filters.layer2_filter import Layer2Filter
from bhav_screener.filters.signals import SignalScreener
from bhav_screener.risk_allocator import RiskAllocator
from bhav_screener.utils.config import Settings, load_settings
from bhav_screener.utils.status import write_status
from bhav_screener.utils.universe import NiftyUniverse

STEPS = ["download", "universe", "layer2", "signals", "allocator"]


class ScreenerPipeline:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self._actions: dict[str, Callable[[], str]] = {
            "download": self._download,
            "universe": self._universe,
            "layer2": self._layer2,
            "signals": self._signals,
            "allocator": self._allocator,
        }

    def run(self, start: str = "download", only: str | None = None) -> dict:
        """Run the steps in order (or just `only`); stop at the first failure."""
        names = [only] if only else STEPS[STEPS.index(start):]
        report: list[dict] = []
        outcome = "ok"
        for name in names:
            began = time.time()
            try:
                summary = self._actions[name]()
                report.append({"step": name, "status": "ok", "summary": summary,
                               "seconds": round(time.time() - began, 1)})
                print(f"[{name}] {summary}")
            except Exception as exc:
                report.append({"step": name, "status": "failed", "error": str(exc),
                               "seconds": round(time.time() - began, 1)})
                print(f"[{name}] FAILED: {exc}")
                outcome = "failed"
                break
        write_status("pipeline", outcome, self.settings, steps=report)
        return {"status": outcome, "steps": report}

    def _download(self) -> str:
        stats = BhavcopyBackfill(self.settings).run()
        if stats["failed"]:
            raise RuntimeError(f"{stats['failed']} day(s) failed to download ({stats}); fix and re-run.")
        return f"raw bhavcopy parquet updated: {stats}"

    def _universe(self) -> str:
        universe = NiftyUniverse(self.settings)  # refreshes the files when missing or older than refresh_days
        symbols = universe.symbols()
        return f"{len(symbols) if symbols else 'all'} symbols in {universe.path.name}"

    def _layer2(self) -> str:
        qualified = Layer2Filter(self.settings).run()
        last_day = qualified["trade_date"].max() if len(qualified) else None
        today = int((qualified["trade_date"] == last_day).sum()) if last_day is not None else 0
        return f"{today} symbols qualified on the latest day -> data/results/layer2/"

    def _signals(self) -> str:
        candidates = SignalScreener(self.settings).run()
        return f"{len(candidates)} triggered candidates -> data/results/layer3/"

    def _allocator(self) -> str:
        plan = RiskAllocator(self.settings).run()
        s = plan["summary"]
        return (f"{s['selected']} of {s['candidates_in']} candidates selected, "
                f"total risk INR {s['total_risk_inr']:,.0f} -> data/results/layer4/")


def main() -> None:
    parser = argparse.ArgumentParser(description="NSE screener pipeline")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--from", dest="start", choices=STEPS, default="download", help="start at this step")
    group.add_argument("--only", choices=STEPS, help="run just this step")
    args = parser.parse_args()
    result = ScreenerPipeline().run(start=args.start, only=args.only)
    raise SystemExit(0 if result["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
