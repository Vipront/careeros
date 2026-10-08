"""Export a small balanced convenience sample for actual human labelling."""
import argparse
import csv
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("root", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.root.resolve()))
from src import db  # noqa: E402

if db.TURSO_URL and db.TURSO_TOKEN:
    con = db.TursoConnection(db.TURSO_URL, db.TURSO_TOKEN)
else:
    import sqlite3
    con = sqlite3.connect((args.root.resolve() / "data/jobs.db").as_uri() + "?mode=ro", uri=True)
try:
    cursor = con.execute("""SELECT id,title,company,location,url,description,profile_type,
        keyword_score,semantic_score,llm_judge_result_json,version_metadata
        FROM jobs WHERE llm_judge_result_json IS NOT NULL
        AND description_available=1 ORDER BY id DESC""")
    columns = [c[0] for c in cursor.description]
    selected, groups = [], {}
    for raw in cursor.fetchall():
        row = dict(zip(columns, raw, strict=True))
        try:
            prediction = json.loads(row["llm_judge_result_json"])
        except (TypeError, ValueError):
            continue
        match = prediction.get("is_match")
        if not isinstance(match, bool):
            continue
        group = (row["profile_type"], match)
        if groups.get(group, 0) >= 2 or len(selected) >= 12:
            continue
        groups[group] = groups.get(group, 0) + 1
        selected.append(row)
finally:
    con.close()
args.output.mkdir(parents=True, exist_ok=False)
with (args.output / "predictions.jsonl").open("w", encoding="utf-8") as handle:
    for row in selected:
        handle.write(json.dumps({"job_id": row["id"], "keyword_score": row["keyword_score"],
                                 "semantic_score": row["semantic_score"],
                                 "llm_judge_result_json": row["llm_judge_result_json"],
                                 "version_metadata": row["version_metadata"]}, ensure_ascii=False) + "\n")
with (args.output / "labels.csv").open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["job_id", "is_match", "reason"])
    writer.writerows([[row["id"], "", ""] for row in selected])
parts = ["# İnsan değerlendirmesi\n\nHer ilan için labels.csv dosyasındaki is_match alanına true veya false, reason alanına kısa gerekçe yaz. Model tahmini bilinçli olarak gösterilmez. Bu küçük dengeli kolaylık örneklemi genel üretim başarısını temsil etmez.\n"]
for row in selected:
    parts.append(f"\n## {row['id']} — {row['title']}\n\n{row['company']} | {row['location']}\n\n{row['url']}\n\n{row['description'] or '(Açıklama yok)'}\n")
(args.output / "ilanlar.md").write_text("\n".join(parts), encoding="utf-8")
print(json.dumps({"sample_size": len(selected), "labels_filled": 0,
                  "sampling": "latest, max two per profile/prediction stratum, max twelve"}))
