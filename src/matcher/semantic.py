import os

os.environ["OMP_NUM_THREADS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import hashlib
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from src.db import get_connection

ROOT = Path(__file__).resolve().parents[2]

PROFILES = ROOT / "data" / "semantic_profiles.json"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
PROFILE_CACHE_SCHEMA = 1
PROFILE_CACHE_DIR = ROOT / "data" / "cache"

PROFILE_STRONG_TERMS = {
    "Wet Lab": [
        "pcr","qpcr","real-time pcr","western blot","immunofluorescence",
        "rna extraction","dna extraction","protein extraction","bca assay",
        "cell culture","molecular biology","moleküler biyoloji",
        "biologie moleculaire","biologie moléculaire",
        "molekularbiologie","wet lab","bioassay",
        "oligonucleotide synthesis","synthesis laboratory",
        "molecular diagnostics","molecular diagnostic","library preparation"
    ],
    "Bioinformatics": [
        "bioinformatics","bioinformatic","bioinformatician","bioinformaticien",
        "bioinformatique","bioinformatik","biyoenformatik","biyoinformatik",
        "computational biology","biologie computationnelle",
        "rechnergestützte biologie","computational genomics",
        "tcga","gtex","ngs","next-generation sequencing","next generation sequencing",
        "gene expression","differential expression","survival analysis",
        "immune infiltration","transcriptomics","cancer genomics",
        "variant analysis","variant calling","rna-seq"
    ],
    "Drug Design": [
        "drug design","drug discovery","molecular docking","autodock vina",
        "swissadme","adme","protox","cheminformatics",
        "protein-ligand","structure-based drug design","molecular modeling",
        "molecular design","computational chemistry","medicinal chemistry",
        "protein design","ligand design","molecular simulation"
    ],
}
OTHER_STRONG_TERMS = ["machine learning","deep learning","representation learning","temporal modeling","user modeling","multimodal","multi-modal","computer science","software engineering","ml scientist","machine learning scientist"]

def load_model():
    print("[semantic] Loading SentenceTransformer model...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    print("[semantic] Model loaded successfully.")
    return model


def _profile_cache_key(profile_bytes):
    digest = hashlib.sha256(MODEL_NAME.encode("utf-8") + b"\0" + profile_bytes).hexdigest()
    return digest


def _load_or_encode_profile_vectors(model, profiles, profile_bytes, cache_dir=None):
    """Load safe cached profile vectors or atomically recompute them.

    The cache avoids repeat profile encoding. The SentenceTransformer is still
    initialized once per semantic subprocess, and job text is encoded per run.
    """
    names = list(profiles)
    profile_texts = [profiles[name]["text"][:2000] for name in names]
    cache_key = _profile_cache_key(profile_bytes)
    dimension_getter = getattr(model, "get_sentence_embedding_dimension", None)
    expected_dimension = dimension_getter() if callable(dimension_getter) else None
    directory = Path(cache_dir) if cache_dir else PROFILE_CACHE_DIR
    cache_path = directory / f"semantic_profiles_{cache_key}.npz"

    if cache_path.is_file():
        try:
            with np.load(cache_path, allow_pickle=False) as cache:
                schema = int(cache["schema"].item())
                saved_key = str(cache["cache_key"].item())
                saved_names = cache["profile_names"].astype(str).tolist()
                vectors = np.asarray(cache["vectors"], dtype=np.float32)
            if (
                schema == PROFILE_CACHE_SCHEMA
                and saved_key == cache_key
                and saved_names == names
                and vectors.ndim == 2
                and vectors.shape[0] == len(names)
                and vectors.shape[1] > 0
                and (expected_dimension is None or vectors.shape[1] == expected_dimension)
                and np.isfinite(vectors).all()
            ):
                print(f"[semantic] Profile embedding cache hit ({len(names)} profiles).")
                return vectors
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
            pass
        print("[semantic] Profile embedding cache is invalid; recomputing.")

    vectors = np.asarray(
        model.encode(profile_texts, normalize_embeddings=True, convert_to_numpy=True),
        dtype=np.float32,
    )
    if vectors.ndim != 2 or vectors.shape[0] != len(names) or not np.isfinite(vectors).all():
        raise ValueError("Encoder returned invalid semantic profile vectors")

    directory.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".semantic_profiles_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez_compressed(
                handle,
                schema=np.asarray(PROFILE_CACHE_SCHEMA, dtype=np.int64),
                cache_key=np.asarray(cache_key),
                profile_names=np.asarray(names, dtype=str),
                vectors=vectors,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, cache_path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass
    print(f"[semantic] Profile embedding cache refreshed ({len(names)} profiles).")
    return vectors

def normalize(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v

def cosine(a,b): return float(np.dot(normalize(a),normalize(b)))

def term_hits(text,terms):
    t = text.lower()
    return [term for term in terms if term in t]

def classify(text, semantic_scores):
    """
    Hybrid domain classifier with role-context protection.

    Explicit domain anchors remain important, but an incidental Wet Lab term
    must not override a clearly non-laboratory commercial role such as medical
    sales / promotion.
    """
    best = max(semantic_scores, key=semantic_scores.get)
    best_sem = semantic_scores[best]

    hits = {
        p: term_hits(text, terms)
        for p, terms in PROFILE_STRONG_TERMS.items()
    }

    if hits["Bioinformatics"] == ["ngs"]:
        hits["Bioinformatics"] = []

    ml_hits = term_hits(
        text,
        [
            "machine learning", "deep learning", "representation learning",
            "temporal modeling", "user modeling", "multimodal",
            "multi-modal", "computer science", "software engineering",
            "ml scientist", "machine learning scientist",
        ],
    )

    t = text.lower()

    # Commercial / customer-facing roles are not Wet Lab just because the
    # posting mentions a scientific subject area.
    non_lab_role_hits = term_hits(
        t,
        [
            "medical sales", "medical representative", "sales representative",
            "sales", "medical promotion", "medical promotional",
            "tıbbi tanıtım", "tanıtım temsilcisi", "satış temsilcisi",
            "business development", "account manager", "commercial",
            "customer success", "customer-facing", "customer facing",
        ],
    )

    counts = {p: len(hits[p]) for p in hits}
    max_count = max(counts.values())

    if non_lab_role_hits:
        non_wet = {
            "Bioinformatics": counts["Bioinformatics"],
            "Drug Design": counts["Drug Design"],
        }
        # If there is no explicit computational/drug-design anchor, this is
        # a non-target commercial role rather than Wet Lab.
        if max(non_wet.values(), default=0) == 0:
            return "Other", best_sem, hits, ml_hits

    if max_count == 0:
        return "Other", best_sem, hits, ml_hits

    candidates = [p for p, c in counts.items() if c == max_count]

    if len(candidates) == 1:
        winner = candidates[0]
        winner_sem = semantic_scores[winner]

        specific_terms = {
            "Bioinformatics": {
                "bioinformatics", "bioinformatician", "bioinformatic",
                "bioinformaticien", "bioinformatique", "bioinformatik",
                "biyoinformatik", "biyoenformatik",
                "computational biology", "genomics", "transcriptomics",
                "next generation sequencing", "next-generation sequencing",
                "variant analysis", "variant calling", "rna-seq",
            },
            "Wet Lab": {
                "pcr", "qpcr", "real-time pcr", "western blot",
                "immunofluorescence", "rna extraction", "dna extraction",
                "protein extraction", "cell culture", "molecular biology",
                "moleküler biyoloji", "biologie moleculaire",
                "biologie moléculaire", "molekularbiologie",
                "molecular diagnostics", "molecular diagnostic", "library preparation",
            },
            "Drug Design": {
                "drug discovery", "drug design", "molecular docking",
                "autodock vina", "swissadme", "adme", "cheminformatics",
                "computational chemistry", "medicinal chemistry",
            },
        }

        if any(term in specific_terms.get(winner, set())
               for term in hits[winner]):
            return winner, winner_sem, hits, ml_hits

        if winner_sem >= 0.25:
            return winner, winner_sem, hits, ml_hits

        return "Other", best_sem, hits, ml_hits

    ranked = sorted(
        candidates,
        key=lambda p: semantic_scores[p],
        reverse=True,
    )
    top = ranked[0]
    second = ranked[1]

    if semantic_scores[top] >= 0.30 and (
        semantic_scores[top] - semantic_scores[second] >= 0.03
    ):
        return top, semantic_scores[top], hits, ml_hits

    by_count = sorted(
        counts,
        key=lambda p: (counts[p], semantic_scores[p]),
        reverse=True,
    )
    if len(by_count) >= 2 and counts[by_count[0]] > counts[by_count[1]]:
        winner = by_count[0]
        if semantic_scores[winner] >= 0.18:
            return winner, semantic_scores[winner], hits, ml_hits

    return "Other", best_sem, hits, ml_hits

def run():
    print("[semantic] Starting semantic stage...")
    print("[semantic] Connecting to DB...")
    con=get_connection()
    try:
        con.execute("PRAGMA foreign_keys=ON")
    except Exception as exc:
        print(f"[semantic] Foreign-key pragma unavailable: {type(exc).__name__}")

    print("[semantic] Fetching jobs...")
    rows=con.execute("""SELECT id,title,company,location,description,requirements_text,education_requirements,experience_requirements,eligibility_text FROM jobs WHERE status='evaluated' AND description_available=1 AND semantic_score IS NULL ORDER BY id LIMIT 40""").fetchall()

    if not rows:
        print("SEMANTIC MATCH: PASS (0 jobs)")
        con.close()
        return

    print(f"[semantic] Found {len(rows)} jobs to process.")
    profile_bytes = PROFILES.read_bytes()
    profiles = json.loads(profile_bytes.decode("utf-8"))
    model = load_model()
    names = list(profiles)
    print("[semantic] Loading profile embeddings...")
    vectors = _load_or_encode_profile_vectors(model, profiles, profile_bytes)

    print(f"[semantic] Batch encoding {len(rows)} jobs...")
    texts = ["\n".join((x or "") for x in row[1:]).strip()[:2000] for row in rows]
    all_vecs = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True, batch_size=16)

    for i, (row, vec) in enumerate(zip(rows, all_vecs, strict=True)):
        job_id = int(row[0])
        text = texts[i]
        sem={n:cosine(vec,vectors[j]) for j,n in enumerate(names)}
        profile,score,hits,ml_hits=classify(text,sem)

        p_val = str(profile)
        # Clip score between 0.0 and 1.0 to satisfy the database CHECK constraint
        # (cosine similarity can be < 0 or > 1.0 due to float inaccuracies)
        s_val = max(0.0, min(1.0, float(max(sem.values()))))
        dt_val = str(datetime.now(timezone.utc).isoformat())

        con.execute("UPDATE jobs SET profile_type=?,semantic_score=?,updated_at=? WHERE id=?",(p_val, s_val, dt_val, job_id))
        print(f"  -> Job {job_id} classified={p_val} (score={s_val:.3f})")

    con.commit()
    con.close()
    print(f"SEMANTIC MATCH: PASS ({len(rows)} jobs)")
if __name__=="__main__":
    run()
