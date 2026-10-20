"""System-Prompts und Prompt-Bausteine. System-Prompts sind STABIL (Prompt-Caching) – keine Zeitstempel!"""

from __future__ import annotations

SYSTEM_EDITOR: str = ""  # Redakteur/Editor für einen Clip -> EditPlan
SYSTEM_MOMENTS: str = ""  # Momente in langem Video finden -> MomentPicks
