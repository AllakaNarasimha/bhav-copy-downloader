import json
import math
from pathlib import Path

import pandas as pd

from bhav_screener.filters.signals import CANDIDATES_NAME
from bhav_screener.utils.config import Settings, load_settings
from bhav_screener.utils.status import write_status
from bhav_screener.utils.universe import NiftyUniverse


class RiskAllocator:
    """Layer 4: pick the top candidates and size them within strict INR risk limits.

    Reads data/results/layer3/layer3_candidates.parquet (best RS score first) and walks it in rank order.
    A candidate is skipped when it would break any of: max positions, per-sector cap, per-trade risk
    cap (quantity is reduced to fit, including the position-value cap) or the daily new-risk cap.
    Writes data/results/layer4/screener_plan_YYYY-MM-DD.json (selected trades and every rejection)
    and screener_report_YYYY-MM-DD.md (the same as readable tables).
    """

    def __init__(self, settings: Settings | None = None, data_dir: str | Path | None = None) -> None:
        self.settings = settings or load_settings()
        cfg = self.settings.data
        base = Path(data_dir) if data_dir is not None else None
        self.candidates_path = cfg.layer_path("layer3", base) / CANDIDATES_NAME
        self.outputs_dir = cfg.layer_path("layer4", base)
        self.sectors = NiftyUniverse(self.settings).sector_map()

    def run(self) -> dict:
        """Build, save and return the plan."""
        try:
            return self._run()
        except Exception as exc:
            write_status("layer4", "failed", self.settings, error=str(exc))
            raise

    def _run(self) -> dict:
        if not self.candidates_path.exists():
            raise RuntimeError(f"{self.candidates_path.name} not found; run SignalScreener first.")
        candidates = self._rank(pd.read_parquet(self.candidates_path))
        risk = self.settings.risk

        selected: list[dict] = []
        rejected: list[dict] = []
        daily_risk = 0.0
        used_capital = 0.0
        per_sector: dict[str, int] = {}
        max_value = risk.capital * risk.max_position_pct / 100

        for row in candidates.itertuples(index=False):
            sector = self.sectors.get(row.symbol, "Unknown")
            qty = self._quantity(row, max_value, risk.capital - used_capital)
            trade_risk = qty * row.risk_per_share
            reason = self._rejection_reason(
                qty, sector, len(selected), per_sector.get(sector, 0), daily_risk + trade_risk
            )

            if reason:
                rejected.append({"symbol": row.symbol, "setup": row.setup, "reason": reason})
                continue

            daily_risk += trade_risk
            used_capital += qty * row.entry
            per_sector[sector] = per_sector.get(sector, 0) + 1
            selected.append(self._trade_record(len(selected) + 1, row, sector, qty, trade_risk))

        as_of = self._as_of(candidates)
        plan = {
            "as_of_date": str(as_of) if as_of else None,
            "limits": {
                "max_positions": risk.max_positions,
                "max_per_sector": risk.max_per_sector,
                "max_risk_per_trade_inr": risk.max_risk_per_trade_inr,
                "max_daily_risk_inr": risk.max_daily_risk_inr,
                "max_position_value_inr": max_value,
                "capital_inr": risk.capital,
                "rank_by": risk.rank_by,
            },
            "summary": {
                "candidates_in": len(candidates),
                "selected": len(selected),
                "total_risk_inr": round(daily_risk, 2),
                "total_position_value_inr": round(sum(t["position_value"] for t in selected), 2),
                "capital_left_inr": round(risk.capital - used_capital, 2),
            },
            "selected": selected,
            "rejected": rejected,
        }

        plan_path = self._save(plan, as_of)
        write_status(
            "layer4",
            "ok" if selected else "empty",
            self.settings,
            as_of_date=as_of,
            plan_file=plan_path,
            **plan["summary"],
            symbols=[t["symbol"] for t in selected],
        )
        return plan

    def _as_of(self, candidates: pd.DataFrame):
        """Screen date from the candidates; with none, from the Layer 3 status so the plan keeps its date."""
        if len(candidates):
            return candidates["trade_date"].max().date()
        status = self.settings.data.status_path / "layer3_status.json"
        if status.exists():
            return json.loads(status.read_text(encoding="utf-8")).get("as_of_date")
        return None

    def _rank(self, candidates: pd.DataFrame) -> pd.DataFrame:
        if self.settings.risk.rank_by == "stop_pct":
            return candidates.sort_values(["risk_pct", "rs_score"], ascending=[True, False])
        return candidates.sort_values("rs_score", ascending=False)

    def _quantity(self, row, max_value: float, free_capital: float) -> int:
        """Shares so the chart stop costs the planned rupees, limited by position size and free cash."""
        return min(
            math.floor(self.settings.risk.max_risk_per_trade_inr / row.risk_per_share),
            math.floor(max_value / row.entry),
            math.floor(free_capital / row.entry),
        )

    def _rejection_reason(
        self, qty: int, sector: str, selected_count: int, sector_count: int, projected_daily_risk: float
    ) -> str | None:
        risk = self.settings.risk
        if selected_count >= risk.max_positions:
            return f"max_positions ({risk.max_positions}) reached"
        if sector_count >= risk.max_per_sector:
            return f"sector cap ({risk.max_per_sector}) reached for {sector}"
        if qty < 1:
            return "one share exceeds the per-trade risk, position-size or remaining-capital limit"
        if projected_daily_risk > risk.max_daily_risk_inr:
            return f"daily risk cap INR {risk.max_daily_risk_inr:,.0f} would be exceeded"
        return None

    def _trade_record(self, rank: int, row, sector: str, qty: int, trade_risk: float) -> dict:
        trade = {
            "rank": rank,
            "symbol": row.symbol,
            "sector": sector,
            "setup": row.setup,
            "entry": row.entry,
            "stop": row.stop,
            "target": row.target,
            "qty": qty,
            "position_value": round(qty * row.entry, 2),
            "risk_inr": round(trade_risk, 2),
            "risk_pct_of_entry": row.risk_pct,
            "rs_score": row.rs_score,
        }
        intraday = self.settings.intraday
        if intraday.enabled:
            trade["exit_policy"] = intraday.exit_policy
            trade["square_off_time"] = intraday.square_off_time
        return trade

    def _save(self, plan: dict, as_of) -> Path:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        stamp = as_of if as_of else "none"
        path = self.outputs_dir / f"screener_plan_{stamp}.json"
        path.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
        (self.outputs_dir / f"screener_report_{stamp}.md").write_text(self._report(plan), encoding="utf-8")
        return path

    @staticmethod
    def _report(plan: dict) -> str:
        s, lim = plan["summary"], plan["limits"]
        lines = [
            f"# Screener report - {plan['as_of_date']}",
            "",
            f"Candidates in: {s['candidates_in']} | Selected: {s['selected']} (max {lim['max_positions']})",
            f"Total risk: INR {s['total_risk_inr']:,.0f} of {lim['max_daily_risk_inr']:,.0f} daily cap | "
            f"Invested: INR {s['total_position_value_inr']:,.0f} of {lim['capital_inr']:,.0f} "
            f"(left {s['capital_left_inr']:,.0f}) | Ranked by: {lim['rank_by']}",
            "",
            "## Selected trades",
            "",
            "| Rank | Symbol | Sector | Setup | Entry | Stop | Stop % | Target | Qty | Position (INR) | Risk (INR) |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for t in plan["selected"]:
            lines.append(
                f"| {t['rank']} | {t['symbol']} | {t['sector']} | {t['setup']} | {t['entry']:,.2f} | "
                f"{t['stop']:,.2f} | {t['risk_pct_of_entry']:.2f} | {t['target']:,.2f} | {t['qty']:,} | "
                f"{t['position_value']:,.0f} | {t['risk_inr']:,.0f} |"
            )
        lines += ["", "## Rejected", "", "| Symbol | Setup | Reason |", "|---|---|---|"]
        lines += [f"| {r['symbol']} | {r['setup']} | {r['reason']} |" for r in plan["rejected"]]
        return "\n".join(lines) + "\n"
