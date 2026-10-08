from src.db import get_connection
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CV_PATH = ROOT / "data" / "master_cv.json"

CV = json.loads(CV_PATH.read_text(encoding="utf-8"))

ALIASES = {
    "PCR": ["pcr", "polymerase chain reaction"],
    "qPCR / Real-Time PCR": ["qpcr", "real-time pcr", "real time pcr", "quantitative pcr"],
    "RNA extraction": ["rna extraction", "rna isolation"],
    "Western blot": ["western blot", "western blotting", "immunoblot"],
    "immunofluorescence": ["immunofluorescence", "if staining", "if assay"],
    "BCA assay": ["bca assay", "bca protein assay"],
    "NGS": ["ngs", "next-generation sequencing", "next generation sequencing"],
    "gene-expression analysis": ["gene expression", "gene-expression", "expression analysis"],
    "TCGA/GTEx": ["tcga", "gtex", "tcga/gtex"],
    "differential expression": ["differential expression", "differential gene expression"],
    "survival analysis": ["survival analysis", "kaplan-meier", "kaplan meier"],
    "immune-infiltration analysis": ["immune infiltration", "immune-infiltration"],
    "bioinformatics": ["bioinformatics", "biyoinformatik", "biyoinformatics"],
    "Python": ["python"],
    "R": ["r scripting", "r programming", "r language"],
    "Linux": ["linux"],
    "AutoDock Vina": ["autodock vina"],
    "molecular docking": ["molecular docking", "docking"],
    "structure-based drug design": ["structure-based drug design", "structure based drug design"],
    "SwissADME": ["swissadme"],
    "ProTox-II": ["protox-ii", "protox ii"],
    "drug discovery": ["drug discovery"],
    "molecular biology": ["molecular biology", "molecular biology and genetics", "moleküler biyoloji"],
    "cell biology": ["cell biology"],
    "molecular diagnostics": ["molecular diagnostics", "molecular diagnosis"],
}

PROFILE_TERMS = {
    "Wet Lab": [
        "molecular biology", "wet lab", "pcr", "qpcr", "western blot",
        "rna extraction", "protein extraction", "immunofluorescence",
        "cell biology", "cell culture", "bca", "laboratory", "lab technician"
    ],
    "Bioinformatics": [
        "bioinformatics", "computational biology", "genomics", "transcriptomics",
        "tcga", "ngs", "gene expression", "data analysis", "python", "r ",
        "biyoenformatik", "biyoinformatik"
    ],
    "Drug Design": [
        "drug discovery", "drug design", "computational drug design",
        "molecular docking", "autodock", "adme", "cheminformatics",
        "protein design", "protein engineering"
    ],
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def alias_hit(alias: str, text: str) -> bool:
    t = normalize(text)
    a = alias.lower()
    if a == "r ":
        return bool(re.search(r"(^|[^a-z])r([^a-z]|$)", t))
    return a in t


def verified_skill_matches(text: str):
    matches = []
    for _, skills in CV.get("skills", {}).items():
        for skill in skills:
            aliases = ALIASES.get(skill, [skill])
            if any(alias_hit(a, text) for a in aliases):
                matches.append(skill)
    return list(dict.fromkeys(matches))


PROFILE_ANCHORS = {
    "Wet Lab": [
        "wet lab", "pcr", "qpcr", "western blot",
        "rna extraction", "protein extraction", "immunofluorescence",
        "cell culture", "bca", "molecular diagnostics",
        "bioassay", "bioassays", "oligonucleotide synthesis",
        "synthesis laboratory"
    ],
    "Bioinformatics": [
        "bioinformatics", "computational biology", "genomics",
        "transcriptomics", "tcga", "gene expression",
        "bioinformatik", "biyoenformatik", "biyoinformatics"
    ],
    "Drug Design": [
        "drug discovery", "drug design", "computational drug design",
        "molecular docking", "autodock", "adme", "cheminformatics",
        "protein design", "molecular design", "medicinal chemistry",
        "structure-based drug design", "protein-ligand"
    ],
}

SUPPORTING_PROFILE_TERMS = {
    "Wet Lab": [
        "molecular biology", "laboratory", "lab technician", "cell biology"
    ],
    "Bioinformatics": [
        "ngs", "data analysis", "python", "r ", "linux"
    ],
    "Drug Design": [
        "protein engineering", "molecular modeling", "molecular simulation"
    ],
}

NON_LAB_ROLE_TERMS = [
    "medical sales", "medical representative", "sales representative",
    "sales", "tıbbi tanıtım", "tanıtım temsilcisi", "satış temsilcisi",
    "clinical research coordinator", "research coordinator",
    "clinical coordinator", "field service engineer", "field service",
    "customer support", "customer success", "account manager",
    "business development", "commercial", "application specialist",
]


def profile_scores(text: str):
    t = normalize(text)
    scores = {}
    non_lab_context = any(term in t for term in NON_LAB_ROLE_TERMS)

    for profile in PROFILE_TERMS:
        anchor_terms = PROFILE_ANCHORS.get(profile, [])
        support_terms = SUPPORTING_PROFILE_TERMS.get(profile, [])

        anchor_hits = sum(1 for term in anchor_terms if term in t)
        support_hits = sum(1 for term in support_terms if term in t)

        # NGS/Python/R/data-analysis are support-only. A generic data role
        # needs a domain-specific Bioinformatics anchor.
        if profile == "Bioinformatics":
            anchor_hits = sum(
                1 for term in (
                    "bioinformatics", "bioinformaticien", "bioinformaticienne",
                    "bioinformatique", "computational biology", "genomics",
                    "transcriptomics", "tcga", "gene expression",
                    "bioinformatik", "biyoenformatik", "biyoinformatics"
                )
                if term in t
            )

        # "Molecular biology" can be incidental in commercial/coordinator/
        # field-service roles. Require an actual lab procedure there.
        if profile == "Wet Lab" and non_lab_context:
            wet_procedure_hits = sum(
                1 for term in (
                    "pcr", "qpcr", "western blot", "rna extraction",
                    "protein extraction", "immunofluorescence", "cell culture",
                    "bca", "wet lab", "molecular diagnostics"
                )
                if term in t
            )
            if wet_procedure_hits == 0:
                anchor_hits = 0

        if anchor_hits == 0:
            scores[profile] = 0
        else:
            scores[profile] = anchor_hits * 3 + min(3, support_hits)

    return scores


def choose_profile(text: str):
    scores = profile_scores(text)
    best = max(scores, key=scores.get)

    # No domain anchor means the job remains Other instead of being forced
    # into a life-science profile by generic programming/data vocabulary.
    if scores[best] == 0:
        return "Other", scores

    # Tie-breaker: keep the legacy profile order.
    return best, scores


def relevance_score(text: str, matches, profile: str) -> float:
    t = normalize(text)

    # Skill overlap is useful, but a few generic CV skills (for example NGS,
    # Python or R) must not dominate relevance by themselves.
    skill_component = min(36, len(matches) * 6)

    # Strong domain anchors establish substantive scientific relevance.
    relevance_anchors = {
        "Wet Lab": [
            "pcr", "qpcr", "western blot", "rna extraction",
            "protein extraction", "immunofluorescence", "cell culture",
            "bca", "wet lab", "molecular diagnostics",
            "bioassay", "bioassays", "oligonucleotide synthesis",
            "synthesis laboratory",
        ],
        "Bioinformatics": [
            "bioinformatics", "computational biology", "genomics",
            "transcriptomics", "tcga", "gene expression",
            "bioinformatik", "biyoenformatik", "biyoinformatics",
        ],
        "Drug Design": [
            "drug discovery", "drug design", "computational drug design",
            "molecular docking", "autodock", "adme", "cheminformatics",
            "protein design", "molecular design", "medicinal chemistry",
            "structure-based drug design", "protein-ligand",
        ],
    }

    domain_hits = sum(
        1 for term in relevance_anchors.get(profile, [])
        if term in t
    )
    domain_component = min(54, domain_hits * 18)

    # Scientific-role context adds limited evidence, but only after a domain
    # anchor exists. Commercial/coordinator/service roles do not receive it.
    scientific_role_terms = [
        "scientist", "scientist", "research assistant", "research associate",
        "researcher", "laboratory technician", "lab technician",
        "expert", "specialist", "intern", "working student",
        "wissenschaftliche hilfskraft", "werkstudent",
    ]
    role_component = 6 if domain_hits > 0 and any(
        term in t for term in scientific_role_terms
    ) else 0

    target_component = 15 if any(
        role.lower() in t for role in CV.get("target_roles", [])
    ) else 0

    return min(100, skill_component + domain_component + role_component + target_component)


def run():
    con = get_connection()
    con.execute("PRAGMA foreign_keys=ON")
    rows = con.execute(
        "SELECT id, title, company, location, description FROM jobs WHERE status='evaluated' ORDER BY id"
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    for job_id, title, company, location, description in rows:
        text = " ".join([title or "", company or "", location or "", description or ""])
        matches = verified_skill_matches(text)
        profile, _ = choose_profile(text)
        score = relevance_score(text, matches, profile)
        con.execute(
            "UPDATE jobs SET profile_type=?, keyword_score=?, match_score=?, updated_at=? WHERE id=?",
            (profile, float(score), float(score), now, job_id),
        )

    con.commit()
    con.close()


if __name__ == "__main__":
    run()
