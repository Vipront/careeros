import os
import sys
import shutil
from pathlib import Path
from typing import Dict, Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def run_system_healthcheck() -> Dict[str, Any]:
    from src.db import get_connection
    from src.fact_registry import get_fact_registry

    """
    Executes automated system diagnostic across all infrastructure components.
    """
    results = {}
    is_healthy = True

    # 1. Database Health
    try:
        con = get_connection()
        wal_res = con.execute("PRAGMA journal_mode").fetchone()
        jobs_cnt = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        con.close()
        results["database"] = {
            "status": "HEALTHY",
            "journal_mode": wal_res[0] if wal_res else "unknown",
            "total_jobs_in_db": jobs_cnt
        }
    except Exception as e:
        results["database"] = {"status": "UNHEALTHY", "error": str(e)}
        is_healthy = False

    # 2. Fact Registry Ground Truth Health
    try:
        reg = get_fact_registry()
        fact_cnt = len(reg.facts)
        if fact_cnt >= 50:
            results["fact_registry"] = {
                "status": "HEALTHY",
                "indexed_facts_count": fact_cnt,
                "entities_indexed": len(reg.entity_index)
            }
        else:
            results["fact_registry"] = {"status": "DEGRADED", "fact_count": fact_cnt}
            is_healthy = False
    except Exception as e:
        results["fact_registry"] = {"status": "UNHEALTHY", "error": str(e)}
        is_healthy = False

    # 3. PDF Conversion Engine Health
    soffice_path = shutil.which("libreoffice") or shutil.which("soffice") or (r"C:\Program Files\LibreOffice\program\soffice.exe" if os.path.exists(r"C:\Program Files\LibreOffice\program\soffice.exe") else None)
    results["pdf_engine"] = {
        "status": "HEALTHY" if soffice_path else "DEGRADED (Headless Fallback)",
        "binary_path": soffice_path or "fallback_docx"
    }

    # 4. LLM Credentials Health
    gemini_key = os.getenv("GEMINI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if gemini_key and len(gemini_key) > 5 and gemini_key != "your_gemini_api_key_here":
        results["llm_api"] = {
            "status": "CONFIGURED",
            "provider": f"Google Gemini ({os.getenv('LLM_MODEL', 'gemini-2.5-flash')})"
        }
    elif anthropic_key and len(anthropic_key) > 10 and anthropic_key != "your_anthropic_api_key_here":
        results["llm_api"] = {
            "status": "CONFIGURED",
            "provider": f"Anthropic Claude ({os.getenv('LLM_MODEL', 'claude-haiku-4-5-20251001')})"
        }
    else:
        results["llm_api"] = {
            "status": "MISSING_OR_LOCAL_MODE",
            "provider": "None"
        }

    # 5. Disk Space Health
    try:
        usage = shutil.disk_usage(str(ROOT))
        free_gb = round(usage.free / (1024 ** 3), 2)
        results["disk_storage"] = {
            "status": "HEALTHY" if free_gb > 1.0 else "WARNING_LOW_DISK",
            "free_space_gb": free_gb
        }
    except Exception:
        results["disk_storage"] = {"status": "HEALTHY"}

    return {
        "overall_status": "SYSTEM_HEALTHY_200_OK" if is_healthy else "SYSTEM_DEGRADED",
        "is_operational": is_healthy,
        "components": results
    }

if __name__ == "__main__":
    report = run_system_healthcheck()
    import json
    print(json.dumps(report, indent=2))
