"""Local SQLite lifecycle registry for agent experiments; no LIVE state exists.

The registry deliberately refuses to promote agents from backtests straight to
trading. A quarantined strategy can be evaluated again in research later.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

SCHEMA="""
CREATE TABLE IF NOT EXISTS agents(
  name TEXT PRIMARY KEY,
  stage TEXT NOT NULL CHECK(stage IN ('RESEARCH','SHADOW','QUARANTINED','RETIRED')),
  created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL REFERENCES agents(name),
  run_id TEXT NOT NULL UNIQUE,
  observed_utc TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('BACKTEST','FORWARD')),
  net_return REAL NOT NULL,
  max_drawdown REAL NOT NULL,
  trade_count INTEGER NOT NULL
);
"""


class AgentRegistry:
    def __init__(self,path: str|Path):
        dest=Path(path)
        dest.parent.mkdir(parents=True,exist_ok=True)
        self.connection=sqlite3.connect(str(dest))
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def register(self,name: str):
        if not name or len(name)>120:
            raise ValueError("Invalid agent name")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO agents VALUES(?,?,?)",
                                    (name,'RESEARCH',datetime.now(timezone.utc).isoformat()))

    def evaluate(self,name: str,run_id: str,source: str,net_return: float,
                 max_drawdown: float,trade_count: int):
        import math
        if source not in {'BACKTEST','FORWARD'}:
            raise ValueError("Evaluation origin must be BACKTEST or FORWARD")
        if not run_id or any(not math.isfinite(x) for x in (net_return,max_drawdown)):
            raise ValueError("Non-finite or missing evaluation")
        if not 0 <= max_drawdown <= 1 or trade_count<0 or net_return < -1:
            raise ValueError("Invalid drawdown/trades/return")
        self.register(name)
        with self.connection:
            self.connection.execute("INSERT INTO evaluations(agent,run_id,observed_utc,source,net_return,max_drawdown,trade_count) VALUES(?,?,?,?,?,?,?)",
                                    (name,run_id,datetime.now(timezone.utc).isoformat(),source,net_return,max_drawdown,trade_count))
            if source=='FORWARD' and (max_drawdown >= .12 or net_return <= -.10):
                self.connection.execute("UPDATE agents SET stage='QUARANTINED' WHERE name=?",(name,))

    def stage(self,name: str) -> str | None:
        row=self.connection.execute("SELECT stage FROM agents WHERE name=?",(name,)).fetchone()
        return row[0] if row else None

    def promote_shadow(self,name: str,*, human_review: bool=False) -> bool:
        """Manual shadow promotion requires validation evidence; never goes LIVE."""
        if not human_review or self.stage(name)!='RESEARCH':
            return False
        summary=self.connection.execute("SELECT COUNT(*),SUM(trade_count),MIN(net_return),MAX(max_drawdown) FROM evaluations WHERE agent=? AND source='BACKTEST'",(name,)).fetchone()
        count,trades,worst,drawdown=summary
        if count is None or count<3 or (trades or 0)<30 or worst is None or worst<-.03 or drawdown > .10:
            return False
        with self.connection:
            self.connection.execute("UPDATE agents SET stage='SHADOW' WHERE name=?",(name,))
        return True
