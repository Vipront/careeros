import json
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
cases = json.loads((ROOT/"data"/"regression_fixture_v1.json").read_text(encoding="utf-8"))["cases"]

print("=== REGRESSION FIXTURE SUMMARY ===")
print("Cases:", len(cases))
print("Expected profiles:", dict(Counter(c["profile"] for c in cases)))
print("Expected knockout:", sum(c["knockout"] is not None for c in cases))
print("Expected LLM eligible:", sum(c["llm"] for c in cases))
print("Expected low/no-LLM:", sum(not c["llm"] for c in cases))
print()
for c in cases:
    print(f"{c['id']}: {c['title']} | profile={c['profile']} | knockout={c['knockout'] or '-'} | llm={c['llm']} | priority={c['priority']}")
