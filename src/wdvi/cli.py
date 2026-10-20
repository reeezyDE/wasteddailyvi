"""Kommandozeile: `wdvi run | init | doctor | discover | process | process-url | render-demo | auth`."""

from __future__ import annotations

import typer

app = typer.Typer(help="WastedDailyVI – virale GTA-6-Clips, vollautomatisch.", no_args_is_help=True)
