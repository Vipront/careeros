import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

ROOT = Path(__file__).resolve().parents[1]

class FactItem:
    def __init__(self, fact_id: str, category: str, text: str, entities: List[str], metrics: List[float], source_section: str):
        self.fact_id = fact_id
        self.category = category
        self.text = text
        self.entities = [e.lower() for e in entities]
        self.metrics = metrics
        self.source_section = source_section

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "category": self.category,
            "text": self.text,
            "entities": self.entities,
            "metrics": self.metrics,
            "source_section": self.source_section
        }

class FactRegistry:
    """
    Production-Grade Fact Store & Indexer for Ground Truth Verification.
    Indexes Master CV facts by Fact ID, Entity, Metric, and Skill Category.
    """
    def __init__(self, master_cv_path: Optional[Path] = None):
        self.master_cv_path = master_cv_path or (ROOT / "data" / "master_cv.json")
        self.facts: Dict[str, FactItem] = {}
        self.entity_index: Dict[str, List[str]] = {} # entity_lower -> [fact_id]
        self.load_and_index()

    def load_and_index(self):
        if not self.master_cv_path.exists():
            return

        data = json.loads(self.master_cv_path.read_text(encoding="utf-8"))

        # 1. Education Facts
        for idx, edu in enumerate(data.get("education", [])):
            fid = f"EDU_{idx+1:02d}"
            text = f"{edu.get('degree')} at {edu.get('institution')} ({edu.get('dates')}, GPA: {edu.get('gpa')})"
            metrics = [float(m) for m in re.findall(r'\b\d+\.\d+\b', edu.get('gpa', ''))]
            entities = [edu.get('degree', ''), edu.get('institution', '')]
            self._register_fact(FactItem(fid, "education", text, entities, metrics, "education"))

        # 2. Research Experience Facts
        for e_idx, exp in enumerate(data.get("research_experience", [])):
            inst = exp.get("institution", "")
            topic = exp.get("topic", "")
            exp_fid_base = f"EXP_{e_idx+1:02d}"

            # Base experience fact
            base_fid = f"{exp_fid_base}_BASE"
            base_text = f"{exp.get('role')} at {inst} ({exp.get('dates')}) - {topic}"
            self._register_fact(FactItem(base_fid, "experience", base_text, [inst, topic, "Example Research Lab", "CXCR4"], [], "research_experience"))

            # Evidence items
            for i_idx, ev in enumerate(exp.get("evidence", [])):
                fid = f"{exp_fid_base}_EV_{i_idx+1:02d}"
                text = ev if isinstance(ev, str) else ev.get("text", "")
                entities = [w for w in ["qPCR", "Western blotting", "BCA", "RNA extraction", "Immunofluorescence", "IF", "CXCR4", "shRNA", "NGS"] if w.lower() in text.lower()]
                self._register_fact(FactItem(fid, "lab_evidence", text, entities + [inst], [], "research_experience"))

        # 3. Project Facts
        for p_idx, proj in enumerate(data.get("projects", [])):
            proj_fid_base = f"THESIS_{p_idx+1:02d}"
            for ev_idx, ev in enumerate(proj.get("evidence", [])):
                fid = f"{proj_fid_base}_EV_{ev_idx+1:02d}"
                text = ev if isinstance(ev, str) else ev.get("text", "")
                metrics = [float(m) for m in re.findall(r'-?\d+\.\d+', text)]
                entities = [w for w in ["AutoDock Vina", "Amentoflavone", "CXCR4", "PDB 3ODU", "SwissADME", "ProTox-II", "TCGA", "GEPIA2", "cBioPortal", "Kaplan-Meier", "TIMER2.0", "STRING", "LUAD", "BRCA", "Plerixafor", "Tyr55"] if w.lower() in text.lower()]
                self._register_fact(FactItem(fid, "project_evidence", text, entities, metrics, proj.get("name", "thesis")))

        # 4. Skills Facts
        skills = data.get("skills", {})
        for cat, items in skills.items():
            for s_idx, skill in enumerate(items):
                fid = f"SKILL_{cat.upper()}_{s_idx+1:02d}"
                text = skill if isinstance(skill, str) else skill.get("text", "")
                self._register_fact(FactItem(fid, f"skill_{cat}", text, [text], [], f"skills.{cat}"))

    def _register_fact(self, fact: FactItem):
        self.facts[fact.fact_id] = fact
        for ent in fact.entities:
            clean_ent = ent.lower().strip()
            if clean_ent:
                if clean_ent not in self.entity_index:
                    self.entity_index[clean_ent] = []
                if fact.fact_id not in self.entity_index[clean_ent]:
                    self.entity_index[clean_ent].append(fact.fact_id)

    def get(self, fact_id: str) -> Optional[FactItem]:
        return self.facts.get(fact_id)

    def find_by_entity(self, entity: str) -> List[FactItem]:
        fids = self.entity_index.get(entity.lower().strip(), [])
        return [self.facts[fid] for fid in fids]

    def match_claim_to_facts(self, claim_text: str) -> List[str]:
        """
        Determines which Fact IDs support a given claim string (1 Claim -> N Evidence).
        """
        matched_fids = []
        claim_lower = claim_text.lower()
        claim_words = set(re.findall(r'\b[a-zA-Z0-9_-]{3,}\b', claim_lower))

        # Check metrics
        claim_metrics = [float(m) for m in re.findall(r'-?\d+\.\d+', claim_text)]

        for fid, fact in self.facts.items():
            fact_words = set(re.findall(r'\b[a-zA-Z0-9_-]{3,}\b', fact.text.lower()))
            overlap = len(claim_words.intersection(fact_words))

            # Metric match check
            metric_match = True
            if claim_metrics and fact.metrics:
                metric_match = any(any(abs(cm - fm) < 0.001 for fm in fact.metrics) for cm in claim_metrics)
            elif claim_metrics and not fact.metrics:
                metric_match = False

            if metric_match and overlap >= 2:
                matched_fids.append(fid)

        return matched_fids

    def get_all_facts(self) -> List[Dict[str, Any]]:
        return [f.to_dict() for f in self.facts.values()]

# Global Singleton instance
_registry_instance = None

def get_fact_registry() -> FactRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = FactRegistry()
    return _registry_instance

if __name__ == "__main__":
    reg = get_fact_registry()
    print(f"FactRegistry loaded {len(reg.facts)} facts successfully!")
    print("Example search for 'CXCR4':", [f.fact_id for f in reg.find_by_entity("CXCR4")])
