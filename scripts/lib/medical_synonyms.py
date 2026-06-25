"""
Medical imaging synonym map for intent-based query construction.

Maps Chinese/English terms into 3 ontology categories used by the intent parser:

  - ``modality``: imaging modality (Ultrasound, CT, MRI, X-Ray, …)
  - ``organ``: anatomical region or organ (Lung, Heart, Brain, …)
  - ``task``: processing task (Segmentation, Classification, Detection, …)

Each category holds a list of ``TermGroup`` objects, where a group is a set of
synonyms that refer to the same entity.  The canonical name (``primary``) is the
English form used in search queries.

Usage::

    >>> im = ImagingOntology()
    >>> im.resolve("超声")           # {"modality": "Ultrasound", "aliases": {"ultrasound", "us"}}
    >>> im.resolve("肺部")           # {"organ": "Lung", "aliases": {"lung", "pulmonary", "pulmo"}}
    >>> im.conflicts("Ultrasound", "modality")
    # {"CT", "MRI", "X-Ray", "PET", "Nuclear Medicine"}

This map is intentionally scoped to *medical imaging* only.  For research
directions outside this domain the intent parser falls back to the original
generic query (``modality``/`organ`/`task` all None).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TermGroup:
    """A set of synonyms that refer to the same entity."""
    primary: str            # Canonical English name (used in search queries)
    aliases: set[str]       # Lowercase aliases (English + Chinese)


@dataclass
class ImagingOntology:
    """In-memory synonym map for medical imaging terms.

    All lookups are case-insensitive.  Chinese, English, and abbreviations
    are indexed so that a query like "US lung segmentation" or "超声肺分割"
    are resolved to the same structured intent.

    Additionally maintains a **hierarchical relation map** (hypernym/hyponym)
    modelled loosely on MeSH tree structures, enabling query expansion:
    a user who says "胸部影像" (chest imaging) should retrieve papers about
    "肺部/纵隔/胸膜" (lung/mediastinum/pleura), not only an exact match.
    """

    # Reverse map: alias_string -> TermGroup (built lazily)
    _alias_map: Optional[dict[str, TermGroup]] = field(default=None, init=False, repr=False)
    _map_dirty: bool = field(default=True, init=False, repr=False)

    # Canonical names grouped by category
    modalities: list[TermGroup] = field(default_factory=list)
    organs: list[TermGroup] = field(default_factory=list)
    tasks: list[TermGroup] = field(default_factory=list)

    # Hierarchical relations (organ-specific)
    # hypernyms[primary] = {superset_primary, ...}   — direct parents
    # hyponyms[primary]    = {subset_primary, ...}   — direct children
    _hypernyms: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)
    _hyponyms: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialise lazy alias map and relation maps."""
        self._alias_map = {}
        self._map_dirty = True
        self._hypernyms = {}
        self._hyponyms = {}

    def _ensure_alias_map(self) -> None:
        """Rebuild the reverse alias map if it's stale."""
        if not self._map_dirty or self._alias_map is None:
            return
        self._alias_map = {}
        for group in self.modalities + self.organs + self.tasks:
            for alias in group.aliases:
                self._alias_map[alias.lower()] = group
        self._map_dirty = False

    def _ensure_hierarchy(self) -> None:
        """Build hypernym/hyponym maps from the organ hierarchy definition."""
        if self._hypernyms:
            return
        # Organ hierarchy: child -> parent (hypernym)
        # Loosely modelled on MeSH Tree Structure 09
        tree: dict[str, str] = {
            # ---- Thorax (chest) ----
            "Lung": "Thorax",
            "Pleura": "Thorax",
            "Mediastinum": "Thorax",
            "Bronchus": "Thorax",
            # ---- Abdomen ----
            "Liver": "Abdomen",
            "Kidney": "Abdomen",
            "Pancreas": "Abdomen",
            "Spleen": "Abdomen",
            "Stomach": "Abdomen",
            "Colorectum": "Abdomen",
            # ---- Head & Neck ----
            "Brain": "Head",
            "Eye": "Head",
            "Thyroid": "Neck",
            # ---- Pelvis ----
            "Prostate": "Pelvis",
            "Cervix": "Pelvis",
            "Ovary": "Pelvis",
            "Bladder": "Pelvis",
            # ---- Cardiovascular ----
            "Heart": "Cardiovascular System",
            "Blood Vessel": "Cardiovascular System",
            # ---- Musculoskeletal ----
            "Bone": "Musculoskeletal System",
            "Muscle": "Musculoskeletal System",
            # ---- Dermatological ----
            "Skin": "Skin and Coverings",
            # ---- Lymphatic ----
            "Lymph Node": "Lymphatic System",
        }
        for child, parent in tree.items():
            self._hypernyms.setdefault(child, set()).add(parent)
            self._hyponyms.setdefault(parent, set()).add(child)

        # Add implicit parent nodes that may not be TermGroups
        implicit_parents = set(tree.values())
        for p in implicit_parents:
            self._hypernyms.setdefault(p, set())
            self._hyponyms.setdefault(p, set())
            for child in tree:
                if tree[child] == p:
                    self._hyponyms[p].add(child)

        # Thorax is a top-level concept (no further parent in our ontology)
        self._hypernyms.setdefault("Thorax", set())
        self._hypernyms.setdefault("Abdomen", set())
        self._hypernyms.setdefault("Head", set())
        self._hypernyms.setdefault("Neck", set())
        self._hypernyms.setdefault("Pelvis", set())
        self._hypernyms.setdefault("Cardiovascular System", set())
        self._hypernyms.setdefault("Musculoskeletal System", set())
        self._hypernyms.setdefault("Skin and Coverings", set())
        self._hypernyms.setdefault("Lymphatic System", set())

        # Map broad Chinese terms to their English ancestor groups
        self._broad_terms: dict[str, str] = {
            "胸部": "Thorax", "胸": "Thorax", "chest": "Thorax", "胸腔": "Thorax",
            "腹部": "Abdomen", "腹": "Abdomen", "abdomen": "Abdomen", "腹腔": "Abdomen",
            "头部": "Head", "头": "Head", "head": "Head",
            "颈部": "Neck", "颈": "Neck", "neck": "Neck",
            "盆腔": "Pelvis", "骨盆": "Pelvis", "pelvis": "Pelvis",
            "心血管": "Cardiovascular System", "心血管系统": "Cardiovascular System",
            "心血管系统": "Cardiovascular System", "cardiovascular": "Cardiovascular System",
            "骨骼": "Musculoskeletal System", "肌肉骨骼": "Musculoskeletal System",
            "肌肉骨骼系统": "Musculoskeletal System", "musculoskeletal": "Musculoskeletal System",
            "皮肤": "Skin", "skin": "Skin",
            "淋巴": "Lymphatic System", "lymphatic": "Lymphatic System",
        }

    def resolve(self, term: str) -> Optional[dict]:
        """Resolve a single term (CN or EN) to a structured dict.

        Returns ``{"category": str, "primary": str, "aliases": set}`` or ``None``.
        """
        if not term:
            return None
        self._ensure_alias_map()
        cleaned = self._strip_punctuation(term).lower()
        group = self._alias_map.get(cleaned) if self._alias_map else None
        if group is None:
            return None
        cat = (
            "modality"
            if group in self.modalities
            else "organ" if group in self.organs else "task"
        )
        return {"category": cat, "primary": group.primary, "aliases": group.aliases}

    def resolve_all(self, text: str) -> dict[str, TermGroup]:
        """Scan ``text`` (any language) and return all matched terms keyed by category.

        At most one entry per category.  If multiple terms map to the same
        category the *longest* match wins (first inserted in case of tie).

        For CJK aliases we use simple substring match with an exception:
        2-character CJK aliases are only accepted if they appear at a CJK
        boundary (preceded or followed by non-CJK or string edge).  This
        prevents ``白质`` from matching inside ``蛋白质``.
        For Latin/English aliases we require word-boundary matching to avoid
        false positives like "us" matching inside "cause".
        """
        if not text:
            return {}
        self._ensure_alias_map()
        cleaned = self._strip_punctuation(text).lower()
        if not self._alias_map:
            return {}

        def _is_cjk_char(ch: str) -> bool:
            if not ch:
                return False
            o = ord(ch)
            return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF

        def _has_cjk(alias: str) -> bool:
            return any(_is_cjk_char(c) for c in alias)

        # Phase 1: find all matches
        all_matches: list[tuple[str, TermGroup, int]] = []
        for alias, group in self._alias_map.items():
            if len(alias) < 2:
                continue
            has_latin = any(c.isascii() for c in alias)
            has_cjk_alias = _has_cjk(alias)

            start = 0
            while True:
                pos = cleaned.find(alias, start)
                if pos == -1:
                    break

                accepted = False
                if has_cjk_alias and len(alias) <= 2:
                    # Short CJK (2 chars): require CJK boundary
                    before_ok = (pos == 0) or (not _is_cjk_char(cleaned[pos - 1]))
                    after_ok = ((pos + len(alias)) >= len(cleaned)) or (
                        not _is_cjk_char(cleaned[pos + len(alias)])
                    )
                    if before_ok or after_ok:
                        accepted = True
                    else:
                        # 2-char CJK alias in the middle of CJK text — this
                        # is common for Chinese research queries like
                        # "帮我找分割相关的论文".  Reject only when the alias
                        # is a substring of a *longer* ontology alias that
                        # appears at an overlapping position (e.g. "白质"
                        # inside "蛋白质" when both are in the ontology).
                        overlapped = False
                        for la_name in self._alias_map:
                            if len(la_name) <= len(alias):
                                continue
                            if alias not in la_name:
                                continue
                            search_start = max(0, pos - len(la_name) + len(alias))
                            la_pos = cleaned.find(la_name, search_start)
                            if (la_pos != -1 and la_pos <= pos
                                    and la_pos + len(la_name) >= pos + len(alias)):
                                overlapped = True
                                break
                        accepted = not overlapped
                elif has_latin and not has_cjk_alias:
                    # Pure Latin alias: use word boundary that works across
                    # languages.  Since Python 3 treats CJK as \w, we need
                    # an explicit approach: check that the alias is at a
                    # non-alphanumeric-ASCII boundary.
                    before_ok = (pos == 0) or (not cleaned[pos - 1].isalnum())
                    after_ok = ((pos + len(alias)) >= len(cleaned)) or (
                        not cleaned[pos + len(alias)].isalnum()
                    )
                    accepted = before_ok or after_ok
                else:
                    # Longer CJK or mixed: substring match is acceptable
                    accepted = alias in cleaned

                if accepted:
                    all_matches.append((alias, group, pos))
                start = pos + 1

        # Phase 2: resolve conflicts per category — longest match wins
        cat_best: dict[str, tuple[int, TermGroup]] = {}
        for alias, group, pos in all_matches:
            cat = (
                "modality"
                if group in self.modalities
                else "organ" if group in self.organs else "task"
            )
            prev_len, prev_grp = cat_best.get(cat, (0, None))
            if len(alias) > prev_len:
                cat_best[cat] = (len(alias), group)
        return {cat: grp for cat, (_, grp) in cat_best.items()}

    def resolve_broad_region(self, text: str) -> Optional[str]:
        """Resolve a broad anatomical region (e.g. "胸部" → "Thorax") from text.

        Returns the parent region name or None if no broad region is detected.
        This is used for query expansion — see ``expand_query_for_organ()``.
        """
        self._ensure_alias_map()
        self._ensure_hierarchy()
        cleaned = self._strip_punctuation(text).lower()
        for alias, ancestor in self._broad_terms.items():
            if len(alias) < 2:
                continue
            if alias in cleaned:
                return ancestor
        return None

    def expand_query_for_organ(self, organ_primary: Optional[str] = None,
                                broad_region: Optional[str] = None,
                                max_depth: int = 2) -> set[str]:
        """Expand an organ entity to include all descendant terms in the hierarchy.

        This is the core query-extension method.  If the user says "胸部影像",
        the broad-region resolver returns ``"Thorax"``.  Then this method returns
        every organ that is a descendant of Thorax in the MeSH-like tree, i.e.
        ``{"Lung", "Pleura", "Mediastinum", "Bronchus"}``.

        Returns a set of primary names for expansion.  Never includes the
        original broad-region node itself (it's not a specific organ).

        Args:
            organ_primary: A specific organ primary name (e.g. ``"Lung"``).  The
                expansion includes this organ itself plus all its hyponyms.
            broad_region: A resolved broad region (e.g. ``"Thorax"``).  The
                expansion includes all descendants.
            max_depth: How deep to traverse the hierarchy.

        Example::

            >>> im.expand_query_for_organ(broad_region="Thorax")
            # {"Lung", "Pleura", "Mediastinum", "Bronchus"}
            >>> im.expand_query_for_organ(organ_primary="Lung")
            # {"Lung"}  (no known hyponyms in our map)
        """
        self._ensure_hierarchy()
        result: set[str] = set()

        def _collect_children(node: str, depth: int) -> None:
            if depth > max_depth:
                return
            children = self._hyponyms.get(node, set())
            for child in children:
                result.add(child)
                _collect_children(child, depth + 1)

        if broad_region and broad_region in self._hyponyms:
            _collect_children(broad_region, 0)
        elif organ_primary:
            result.add(organ_primary)
            _collect_children(organ_primary, 0)

        return result

    def expand_query_with_synonyms(self, primary: str,
                                    category: str) -> set[str]:
        """Return ALL alias strings (not just primary) for a given entity.

        Combines synonyms from the synonym map AND all hierarchical descendants
        for organ entities, producing a comprehensive expansion set for query
        construction.

        Returns a set of lowercase strings to be added to the search query.
        """
        self._ensure_alias_map()
        self._ensure_hierarchy()
        all_aliases: set[str] = set()

        for group in (self.modalities + self.organs + self.tasks):
            if group.primary == primary:
                all_aliases.update(group.aliases)
                break

        # For organ entities, also expand to sub-regions
        if category == "organ" and primary:
            descendants = self.expand_query_for_organ(organ_primary=primary)
            for desc in descendants:
                for g in self.organs:
                    if g.primary == desc:
                        all_aliases.update(g.aliases)
                        break

        return all_aliases

    def conflicts(self, primary: str, category: str) -> set[str]:
        """Return sibling primary names in the same category (i.e. conflicting terms).

        Example::

            >>> im.conflicts("Ultrasound", "modality")
            # {"CT", "MRI", "X-Ray", "PET", "Nuclear Medicine"}
        """
        if category == "modality":
            return {g.primary for g in self.modalities if g.primary != primary}
        if category == "organ":
            return {g.primary for g in self.organs if g.primary != primary}
        return set()

    def negation_keywords(self, primary: str, category: str) -> set[str]:
        """Return all alias strings of conflicting terms (for building NOT clauses).

        Example: if user wants "Ultrasound", this returns every alias of CT, MRI, X-Ray, …
        """
        sibs = self.conflicts(primary, category)
        groups = self.modalities if category == "modality" else self.organs
        result: set[str] = set()
        for g in groups:
            if g.primary in sibs:
                result.update(g.aliases)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_punctuation(s: str) -> str:
        s = s.strip()
        s = re.sub(r"[（)[（）]，,；;\s]+", " ", s)
        return s.strip()


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------

def _build_ontology() -> ImagingOntology:
    """Construct the full medical imaging ontology."""
    im = ImagingOntology()

    # ---- Modalities ----
    im.modalities = [
        TermGroup("Ultrasound", {"ultrasound", "超声", "b超", "彩超", "us", "echo", "echography", "echocardiography"}),
        TermGroup("CT", {"ct", "computed tomography", "计算机断层扫描", "cat扫描"}),
        TermGroup("MRI", {"mri", "magnetic resonance imaging", "磁共振", "核磁共振", "核磁"}),
        TermGroup("X-Ray", {"x-ray", "xray", "radiography", "放射", "dr", "cr", "x光", "xray"}),
        TermGroup("PET", {"pet", "positron emission tomography", "正电子发射断层扫描", "pet-ct", "pet-mri"}),
        TermGroup("Nuclear Medicine", {"nuclear medicine", "核医学", "gamma", "spect"}),
        TermGroup("Optical Coherence Tomography", {"oct", "optical coherence tomography", "光学相干断层扫描", "扫频oct", "频域oct"}),
        TermGroup("Endoscopy", {"endoscopy", "内镜", "内窥镜", "colonoscopy", "gastroscopy"}),
        TermGroup("EEG", {"eeg", "electroencephalography", "脑电图"}),
        TermGroup("ECG", {"ecg", "ekg", "electrocardiography", "心电图"}),
    ]

    # ---- Organs / Anatomical Regions ----
    im.organs = [
        TermGroup("Lung", {"lung", "lungs", "肺", "肺部", "pulmonary", "pulmo", "pleura", "胸膜"}),
        TermGroup("Heart", {"heart", "心脏", "心脏", "cardiac", "cardio", "myocardium", "ventricle", "atrium", "心室", "心房"}),
        TermGroup("Brain", {"brain", "脑", "大脑", "cerebral", "cortex", "白质", "灰质"}),
        TermGroup("Liver", {"liver", "肝", "肝脏", "hepatic"}),
        TermGroup("Kidney", {"kidney", "kidneys", "肾", "肾脏", "renal"}),
        TermGroup("Pancreas", {"pancreas", "胰腺", "胰", "pancreatic"}),
        TermGroup("Spleen", {"spleen", "脾", "脾脏", "splenic"}),
        TermGroup("Stomach", {"stomach", "胃", "gastric", "stomach", "胃"}),
        TermGroup("Bone", {"bone", "bones", "骨", "骨骼", "skeletal", "vertebra", "脊椎", "pelvis", "骨盆"}),
        TermGroup("Blood Vessel", {"blood vessel", "blood vessels", "血管", "vascular", "artery", "vein", "aorta", "coronary artery", "冠状动脉"}),
        TermGroup("Prostate", {"prostate", "前列腺", "prostatic"}),
        TermGroup("Ovary", {"ovary", "ovaries", "卵巢", "ovarian"}),
        TermGroup("Breast", {"breast", "乳腺", "breasts", "mammary"}),
        TermGroup("Thyroid", {"thyroid", "甲状腺", "thyroïd", "thyroidal"}),
        TermGroup("Cervix", {"cervix", "宫颈", "子宫", "uterus", "cervical", "womb"}),
        TermGroup("Colorectum", {"colorectum", "colonic", "colon", "rectum", "大肠", "结直肠", "rectal"}),
        TermGroup("Skin", {"skin", "皮肤", "dermal", "epidermis"}),
        TermGroup("Eye", {"eye", "eyes", "眼", "眼部", "ocular", "retina", "视网膜", "ophthalmic"}),
        TermGroup("Lymph Node", {"lymph node", "lymph nodes", "淋巴结", "lymphatic"}),
        TermGroup("Muscle", {"muscle", "肌肉", "muscular", "myocardial"}),
    ]

    # ---- Tasks ----
    im.tasks = [
        TermGroup("Segmentation", {"segmentation", "分割", "segmenting", "segment", "semantic segmentation", "语义分割", "instance segmentation", "实例分割", "instance segmentation", "实例分割", "medical image segmentation", "医学图像分割"}),
        TermGroup("Classification", {"classification", "分类", "categorization", "image classification", "图像分类", "disease classification", "疾病分类"}),
        TermGroup("Detection", {"detection", "检测", "detecting", "object detection", "目标检测", "lesion detection", "病灶检测", "tumor detection", "肿瘤检测"}),
        TermGroup("Registration", {"registration", "配准", "image registration", "图像配准", "spatial registration", "空间配准"}),
        TermGroup("Reconstruction", {"reconstruction", "重建", "image reconstruction", "图像重建", "tomographic reconstruction"}),
        TermGroup("Enhancement", {"enhancement", "增强", "denoising", "去噪", "super-resolution", "超分辨率", "sharpening", "锐化"}),
        TermGroup("Synthesis", {"synthesis", "生成", "image synthesis", "图像生成", "domain adaptation", "域适应", "modality synthesis", "模态生成"}),
        TermGroup("Quantification", {"quantification", "定量", "measurement", "测量", "morphometry", "形态测量", "volume analysis", "体积分析"}),
        TermGroup("Flow", {"flow", "血流", "血流动力学", "doppler flow", "多普勒血流"}),
    ]

    return im


# Module-level singleton
_im_ontology: Optional[ImagingOntology] = None


def get_ontology() -> ImagingOntology:
    """Return the module-level singleton ImagingOntology."""
    global _im_ontology
    if _im_ontology is None:
        _im_ontology = _build_ontology()
    return _im_ontology


# ---------------------------------------------------------------------------
# Entity deficiency detection
# ---------------------------------------------------------------------------

def check_intent_deficiency(intent: dict, original_query: str = "") -> dict:
    """Check if a medical-imaging intent lacks critical entities for effective search.

    When the user says e.g. "帮我找近年分割相关的论文" (task only, no modality
    or organ), running the pipeline blindly would return hundreds of loosely
    related papers across all modalities and organs.  This function detects such
    cases so the caller can ask the user to narrow the scope before searching.

    Args:
        intent: dict with keys ``modality``, ``organ``, ``task`` (each str|None).
        original_query: the user's original query string (unused; reserved for
            future confidence scoring based on query length / keyword density).

    Returns:
        dict with keys:
          - **deficient** (bool): whether the intent is too vague for a focused search.
          - **severity** (str): ``"critical"``, ``"partial"``, or ``"none"``.
          - **missing** (list[str]): which entity types are missing
            (``"modality"``, ``"organ"``).
          - **has_medical_terms** (bool): whether any medical-imaging entity was
            extracted at all.
          - **suggestion_cn** (str): follow-up question in Chinese.
          - **suggestion_en** (str): follow-up question in English.

    Deficiency thresholds:
        ==================== ========= ======= ===== ====================
        Scenario              modality   organ   task   Action
        ==================== ========= ======= ===== ====================
        Critical deficiency   None       None    Any   **MUST ask** user
        Partial deficiency    None       Present Present Ask user
        Partial deficiency    Present    None    Present Ask user
        No deficiency         Present    Present Any    Proceed with search
        Non-medical query     None       None    None   Proceed normally
        ==================== ========= ======= ===== ====================

    Examples::

        >>> check_intent_deficiency({"modality": None, "organ": None, "task": "Segmentation"})
        {"deficient": True, "severity": "critical", "missing": ["modality", "organ"], ...}
        >>> check_intent_deficiency({"modality": "Ultrasound", "organ": None, "task": "Segmentation"})
        {"deficient": True, "severity": "partial", "missing": ["organ"], ...}
        >>> check_intent_deficiency({"modality": "CT", "organ": "Lung", "task": "Segmentation"})
        {"deficient": False, "severity": "none", "missing": [], ...}
        >>> check_intent_deficiency({"modality": None, "organ": None, "task": None})
        {"deficient": False, "severity": "none", "has_medical_terms": False, ...}
    """
    modality = intent.get("modality")
    organ = intent.get("organ")
    task = intent.get("task")

    has_medical_terms = modality is not None or organ is not None or task is not None

    if not has_medical_terms:
        return {
            "deficient": False,
            "severity": "none",
            "missing": [],
            "has_medical_terms": False,
            "suggestion_cn": "",
            "suggestion_en": "",
        }

    missing: list[str] = []
    if modality is None:
        missing.append("modality")
    if organ is None:
        missing.append("organ")

    # ---- severity classification ----
    if modality is None and organ is None:
        severity = "critical"
        deficient = True
    elif (modality is None or organ is None) and task is not None:
        severity = "partial"
        deficient = True
    else:
        # Both modality and organ present (task may be None — still searchable)
        severity = "none"
        deficient = False

    # ---- build follow-up question templates ----
    task_cn = task if task else "医学影像"
    task_en = (task or "medical imaging").lower()

    missing_cn_parts: list[str] = []
    missing_en_parts: list[str] = []
    if "modality" in missing:
        missing_cn_parts.append("医学影像模态（如超声、CT、MRI、X-Ray）")
        missing_en_parts.append("imaging modality (e.g., ultrasound, CT, MRI, X-Ray)")
    if "organ" in missing:
        missing_cn_parts.append("器官或部位（如肺部、肝脏、脑部、心脏、肾脏）")
        missing_en_parts.append("anatomical region or organ (e.g., lung, liver, brain, heart, kidney)")

    missing_cn = "和".join(missing_cn_parts)
    missing_en = " and ".join(missing_en_parts)

    suggestion_cn = (
        f"我可以帮您查找{task_cn}相关的论文。"
        f"为了更准确地缩小检索范围，请问您关注的是哪种{missing_cn}呢？"
    )

    suggestion_en = (
        f"I can help find papers related to {task_en}. "
        f"To narrow the search, could you specify the {missing_en}?"
    )

    return {
        "deficient": deficient,
        "severity": severity,
        "missing": missing,
        "has_medical_terms": True,
        "suggestion_cn": suggestion_cn,
        "suggestion_en": suggestion_en,
    }
