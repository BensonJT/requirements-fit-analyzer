"""JD keyword extraction for application packages: section trim, then four NLP layers, then a quality filter.

Moved here from resume_ide_v2 (jd_bucketize.py + backend/src/services/jd_processor.py) on 2026-10-30, when that
project was retired. Behaviour is the same except:
  * Step 0 (new): the JD is cut to its duties and requirements with the finder's own section splitter
    (backend.finder.requirements.split_requirements), so EEO, pay, benefits and the company blurb never reach the
    extractor or the LLM. Falls back to the full text when the splitter finds nothing to keep.
  * No database write and no skill_taxonomy_learned.json log (both belonged to the retired app).
  * spaCy is parsed once per JD instead of once per layer (same doc, same results).

Layers:
  1  taxonomy match (skill_taxonomy.json, hard and soft skills)
  2  spaCy noun phrases (with bigram / trigram sub-phrases)
  3  spaCy verb + object patterns
  4  token patterns (camelCase, ACRONYMS, dotted, hyphenated, slash, parenthetical)
  5  standalone adjectives and verbs
spaCy (en_core_web_sm) is optional for the rest of jobsearch and imported lazily here.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

_DATA = Path(__file__).resolve().parent
_TAXONOMY_PATH = _DATA / "skill_taxonomy.json"
VERB_CSV_PATH = _DATA / "action_verbs.csv"   # written by the vault's refresh_action_verbs.sh

# Sections of a JD worth mining for resume keywords. "body" is a posting with no headings at all (its boilerplate
# is still stripped by the splitter); "intro" (company overview) is dropped.
KEEP_SECTIONS = ("responsibility", "required", "preferred", "persona", "body")

# Posting logistics the splitter can still file under a kept heading (it made a "preferred" unit of Microsoft's
# trailing pay paragraph, 2026-10-30): pay, benefits, EEO, accommodation, posting window, hashtags.
_LOGISTICS_LINE = re.compile(
    r"\b(?:base pay|pay range|salary range|compensation range|per year|per hour|benefits? (?:and|&) "
    r"(?:other )?(?:compensation|pay)|equal (?:employment )?opportunity|reasonable accommodation|"
    r"accommodations?|open for a minimum|applications? (?:will be )?accepted|until the position is filled|"
    r"e-?verify)\b|^#\w", re.IGNORECASE)

_nlp = None
_hard_flat: dict[str, str] | None = None
_soft_flat: dict[str, str] | None = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy  # noqa: PLC0415
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def _load_taxonomy() -> None:
    global _hard_flat, _soft_flat
    if _hard_flat is not None:
        return
    tax = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    _hard_flat = {s.lower(): s for skills in tax["hard_skills"].values() for s in skills}
    _soft_flat = {s.lower(): s for skills in tax["soft_skills"].values() for s in skills}


def clean_text(text: str) -> str:
    """Strip HTML tags, normalize whitespace, trim."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Step 0: keep only duties and requirements ─────────────────────────────────

def duties_and_requirements(jd_text: str) -> tuple[str, dict[str, int]]:
    """(text, chars per section kept or dropped). The text is the KEEP_SECTIONS lines, sub-headings included, in
    document order. A line is kept when the splitter made a requirement unit from it (its classifier drops pay
    lines, hashtags and notices that sit under a kept heading) or when it is a sub-heading. Returns the full JD
    and an empty dict when nothing would be kept."""
    from backend.finder.requirements import section_lines, split_requirements  # noqa: PLC0415

    sources = [u.source for u in split_requirements(jd_text)]
    sizes: dict[str, int] = {}
    kept: list[str] = []
    for section, line, is_subheading in section_lines(jd_text):
        if _LOGISTICS_LINE.search(line) or not (is_subheading or any(src in line for src in sources)):
            section = "boilerplate"
        sizes[section] = sizes.get(section, 0) + len(line)
        if section in KEEP_SECTIONS:
            kept.append(line)
    if not kept:
        return jd_text, {}
    return "\n".join(kept), sizes


# ── Layer helpers (unchanged from jd_processor) ───────────────────────────────

def _count_occurrences(text_lower: str, keyword_lower: str) -> int:
    return len(re.findall(r"\b" + re.escape(keyword_lower) + r"\b", text_lower))


_DOTTED_EXCLUDE = {"i.e", "e.g", "u.s", "a.m", "p.m", "vs.", "etc.", "inc.", "corp."}
_STOPWORDS = {
    "the", "and", "for", "with", "are", "our", "you", "will", "has", "have",
    "not", "but", "can", "all", "may", "who", "how", "its", "per", "any",
    "new", "also", "this", "that", "from", "they", "them", "their",
}


def _detect_tech_patterns(text: str, known_lower: set[str]) -> list[str]:
    """Layer 4: structured tokens not already matched by the taxonomy."""
    found = []
    for match in re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", text):          # camelCase / PascalCase
        if match.lower() not in known_lower:
            found.append(match)
    for match in re.findall(r"\b[A-Z]{2,6}\b", text):                            # ACRONYMS
        if match.lower() not in known_lower and match.lower() not in _STOPWORDS:
            found.append(match)
    for match in re.findall(r"\b\w+\.\w+\b", text):                              # Node.js, ASP.NET
        if match.lower() not in known_lower and match.lower() not in _DOTTED_EXCLUDE:
            found.append(match)
    for match in re.findall(r"\b[a-z]{3,}-[a-z]{3,}(?:-[a-z]{3,})?\b", text, re.IGNORECASE):  # cross-functional
        lower = match.lower()
        if lower not in known_lower and not all(w in _STOPWORDS for w in lower.split("-")):
            found.append(lower)
    for match in re.findall(r"\b[A-Za-z0-9]{2,8}/[A-Za-z0-9]{2,8}\b", text):     # CI/CD
        if match.lower() not in known_lower:
            found.append(match)
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9 ._-]{1,30})\s*\(([^)]{2,60})\)", text):  # "AWS (EC2, S3)"
        for sub in re.split(r"[,;/]", match.group(2)):
            sub = sub.strip()
            if 2 <= len(sub) <= 30 and sub.lower() not in known_lower:
                found.append(sub)
    return list(set(found))


_TECH_INDICATOR_WORDS = {
    "algorithm", "analytics", "api", "architecture", "automation", "backend",
    "cloud", "code", "coding", "compute", "container", "data", "database",
    "deployment", "design", "development", "devops", "distributed", "docker",
    "engineering", "framework", "frontend", "function", "infrastructure",
    "integration", "interface", "kubernetes", "language", "library", "machine",
    "microservice", "migration", "model", "monitoring", "network", "nlp",
    "optimization", "platform", "pipeline", "programming", "protocol",
    "query", "scalable", "script", "security", "server", "software",
    "sql", "stack", "system", "technology", "testing", "tool", "version",
    "visualization", "workflow",
}
_SOFT_INDICATOR_WORDS = {
    "alignment", "collaboration", "communication", "coordination", "culture",
    "decision", "delivery", "engagement", "executive", "facilitation",
    "feedback", "influence", "initiative", "innovation", "leadership",
    "management", "mentoring", "negotiation", "organization", "partnership",
    "people", "performance", "planning", "presentation", "prioritization",
    "problem", "process", "program", "project", "relationship", "reporting",
    "requirement", "resource", "responsibility", "roadmap", "stakeholder",
    "strategy", "team", "transformation", "vision",
}
_GENERIC_STARTERS = {
    "a", "an", "the", "this", "that", "our", "your", "their", "its", "we",
    "you", "they", "it", "which", "who", "what", "where", "when", "how",
    "all", "any", "some", "other", "such", "both", "each", "every",
}
_GENERIC_SINGLE_TERMS = {
    "experience", "background", "knowledge", "understanding", "ability",
    "skill", "skills", "expertise", "proficiency", "role",
    "requirement", "responsibility", "opportunity", "position",
    "candidate", "applicant", "individual", "member", "person", "year",
    "years", "environment", "area", "field", "sector", "industry",
    "company", "organization", "product",
    "result", "impact", "value", "level", "type", "way",
    "part", "set", "range", "variety", "number", "series",
    "requirements", "modeling", "approach",
}
_PREPOSITION_STOPWORDS = {
    "or", "and", "of", "in", "to", "for", "with", "on", "at", "by",
    "from", "into", "about", "through", "strong", "excellent", "good",
    "great", "proven", "deep", "solid", "broad", "key", "main",
}


def _classify_phrase(phrase_lower: str) -> str | None:
    """'hard', 'soft', or None (skip)."""
    word_list = phrase_lower.split()
    words = set(word_list)
    first_word = word_list[0] if word_list else ""
    if first_word in _GENERIC_STARTERS or first_word in _PREPOSITION_STOPWORDS:
        return None
    if len(word_list) > 4 or len(phrase_lower) < 4:
        return None
    if len(word_list) == 1 and phrase_lower in _GENERIC_SINGLE_TERMS:
        return None
    if all(w in _STOPWORDS | _GENERIC_STARTERS for w in words):
        return None
    has_tech = bool(words & _TECH_INDICATOR_WORDS)
    has_soft = bool(words & _SOFT_INDICATOR_WORDS)
    if has_soft and not has_tech:
        return "soft"
    return "hard"   # tech, ambiguous, or neither: over-extract rather than miss


def _extract_noun_phrases(doc, known_lower: set[str]) -> list[dict]:
    """Layer 2."""
    results, seen = [], set()
    for chunk in doc.noun_chunks:
        full_phrase = chunk.text.lower().strip()
        candidates = [full_phrase, chunk.root.lemma_.lower(), chunk.root.text.lower()]
        words = full_phrase.split()
        if len(words) >= 3:
            candidates += [f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)]
        if len(words) >= 4:
            candidates += [f"{words[i]} {words[i + 1]} {words[i + 2]}" for i in range(len(words) - 2)]
        for candidate in candidates:
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if not candidate or candidate in seen or candidate in known_lower:
                continue
            if not 4 <= len(candidate) <= 60:
                continue
            skill_type = _classify_phrase(candidate)
            if skill_type:
                results.append({"term": candidate, "type": skill_type})
                seen.add(candidate)
    return results


_REQUIREMENT_VERBS = {
    "analyze", "architect", "automate", "build", "collaborate", "communicate",
    "coordinate", "create", "define", "deploy", "design", "develop", "drive",
    "ensure", "establish", "evaluate", "execute", "facilitate", "implement",
    "improve", "integrate", "lead", "leverage", "maintain", "manage",
    "monitor", "optimize", "oversee", "own", "present", "prioritize",
    "provide", "report", "resolve", "scale", "scope", "support", "transform",
    "use", "utilize", "work",
}


def _extract_verb_objects(doc, known_lower: set[str]) -> list[dict]:
    """Layer 3."""
    results, seen = [], set()
    for token in doc:
        if token.lemma_.lower() not in _REQUIREMENT_VERBS:
            continue
        for child in token.children:
            if child.dep_ not in ("dobj", "pobj", "nsubjpass", "attr"):
                continue
            phrase = re.sub(r"\s+", " ", doc[child.left_edge.i: child.right_edge.i + 1].text.lower()).strip()
            if not phrase or phrase in seen or phrase in known_lower or not 4 <= len(phrase) <= 50:
                continue
            skill_type = _classify_phrase(phrase)
            if skill_type:
                results.append({"term": phrase, "type": skill_type})
                seen.add(phrase)
    return results


def _extract_adjectives_and_verbs(doc, known_lower: set[str]) -> list[dict]:
    """Layer 5. Surface form preferred (counting is exact-form); deduped on lemma."""
    results, seen = [], set()
    for token in doc:
        if token.pos_ in ("ADJ", "VERB") and len(token.lemma_) >= 4:
            lemma, surface = token.lemma_.lower(), token.text.lower()
            if lemma in _STOPWORDS or lemma in known_lower or lemma in seen:
                continue
            results.append({"term": surface if len(surface) >= 4 else lemma, "type": "soft"})
            seen.add(lemma)
    return results


def process_jd(raw_text: str) -> list[dict]:
    """All five layers. Returns [{keyword, count, required, skill_type}], hard skills then soft, by count."""
    _load_taxonomy()
    cleaned = clean_text(raw_text)
    text_lower = cleaned.lower()

    hard, soft = [], []
    matched_hard, matched_soft = set(), set()
    for kw_lower, kw in sorted(_hard_flat.items(), key=lambda x: -len(x[0])):
        count = _count_occurrences(text_lower, kw_lower)
        if count:
            hard.append({"keyword": kw, "count": count, "required": count >= 3, "skill_type": "hard"})
            matched_hard.add(kw_lower)
    for kw_lower, kw in sorted(_soft_flat.items(), key=lambda x: -len(x[0])):
        if kw_lower in matched_hard:
            continue
        count = _count_occurrences(text_lower, kw_lower)
        if count:
            soft.append({"keyword": kw, "count": count, "required": count >= 3, "skill_type": "soft"})
            matched_soft.add(kw_lower)
    known = matched_hard | matched_soft

    doc = _get_nlp()(cleaned)
    nlp_candidates = (_extract_noun_phrases(doc, known) + _extract_verb_objects(doc, known)
                      + _extract_adjectives_and_verbs(doc, known))
    seen = set(known)
    for entry in nlp_candidates:
        term = re.sub(r"^[\s,;:.\-/]+", "", entry["term"]).strip()
        if not term or not term[0].isalnum() or term.lower() in seen:
            continue
        count = _count_occurrences(text_lower, term.lower())
        if not count:
            continue
        item = {"keyword": term, "count": count, "required": count >= 3, "skill_type": entry["type"]}
        (hard if entry["type"] == "hard" else soft).append(item)
        seen.add(term.lower())
    for term in _detect_tech_patterns(cleaned, known):
        term = re.sub(r"^[\s,;:.\-/]+", "", term).strip()
        if not term or not term[0].isalnum():
            continue
        term_lower = term.lower()
        if term_lower.split()[0] in {"or", "and", "of", "in", "to", "for", "with", "on", "at", "by", "from"}:
            continue
        if term_lower in seen:
            continue
        count = _count_occurrences(text_lower, term_lower)
        if count:
            hard.append({"keyword": term, "count": count, "required": count >= 3, "skill_type": "hard"})
            seen.add(term_lower)

    hard.sort(key=lambda x: -x["count"])
    soft.sort(key=lambda x: -x["count"])
    return hard + soft


# ── Quality filter (unchanged from jd_bucketize._filter_for_groq) ─────────────

_FRAGMENT_PUNCT = re.compile(r"[,;:\"'\(\)\/\\]|[–—]")

# Single words that pass regardless of JD frequency; every other single word needs 3+ mentions.
_CERT_TOOL_WHITELIST = {
    "agile", "scrum", "pmp", "csm", "safe", "cgfm", "itil", "prince2",
    "pmbok", "lean", "dmaic", "kaizen", "kanban",
    "sharepoint", "powerpoint", "teams", "visio", "excel", "word", "outlook",
    "analytics", "governance", "audit", "security", "monitoring", "automation",
    "documentation", "transparency", "onboarding", "leadership", "management",
    "communication", "transformation", "performance", "alignment", "readiness",
    "adoption", "efficiency", "engagement", "execution", "integration",
    "metrics", "reporting", "standards", "controls", "insights", "finance",
    "research", "analysis",
}

# Fallback when action_verbs.csv is absent. The CSV comes from Forester's forester.verb_lexicon through the
# vault's Professional/Resources/refresh_action_verbs.sh; verbs are curated there, never here.
_VERB_STARTERS_BASELINE: frozenset[str] = frozenset({
    "provide", "support", "craft", "lead", "conduct", "facilitate",
    "develop", "ensure", "create", "present", "deliver", "identify",
    "maintain", "establish", "enable", "drive", "define", "gather",
    "update", "confirm", "pursue", "guide", "track", "include", "build",
    "assess", "align", "embed", "apply", "review", "prepare", "leverage",
    "utilize", "execute", "oversee", "coordinate", "collaborate",
    "implement", "perform", "analyze", "design", "optimize", "monitor",
    "evaluate", "refine", "refresh", "improve", "advance", "achieve",
    "demonstrate", "bring", "work", "make", "take", "keep", "set",
    "write", "read", "report", "run", "own", "serve", "act", "help",
    "assist", "contribute", "use", "manage", "communicate",
})


def _load_verb_starters() -> frozenset[str]:
    """base_verb plus gerund forms (past tense excluded: it doubles as an adjective, 'structured decision
    making'). Both columns are read, so a one-column CSV would silently drop the gerunds."""
    if not VERB_CSV_PATH.exists():
        return _VERB_STARTERS_BASELINE
    verbs: set[str] = set()
    with open(VERB_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            bv = (row.get("base_verb") or "").strip().lower()
            if bv and " " not in bv:
                verbs.add(bv)
            if (row.get("verb_form") or "").strip().lower() == "gerund":
                v = (row.get("verb") or "").strip().lower()
                if v and " " not in v:
                    verbs.add(v)
    return frozenset(verbs | _VERB_STARTERS_BASELINE) if verbs else _VERB_STARTERS_BASELINE


_VERB_STARTERS = _load_verb_starters()

_SINGLE_WORD_NOISE = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "into", "through", "about", "over",
    "also", "well", "per", "up", "out", "off", "has", "have", "had", "not",
    "be", "been", "being", "do", "does", "did", "will", "would", "should",
    "could", "may", "might", "shall", "must", "can", "need", "let", "make",
    "guidehouse", "etc", "e.g", "i.e", "role", "roles", "desire", "nature",
    "elements", "steps", "sites", "input", "points", "topics", "areas",
    "items", "needs", "quick", "next", "more", "other", "each", "any",
    "all", "both", "full", "new", "key", "main", "such", "current", "future",
    "related", "required", "multiple", "specific", "active", "obtain",
    "public", "three", "apply", "clear", "fluid", "align", "embed", "build",
    "learn", "assess", "manage", "define", "gather", "update", "confirm",
    "pursue", "craft", "guide", "track", "include", "identify", "provide",
    "develop", "deliver", "ensure", "support", "create", "enable", "drive",
    "lead", "maintain", "present", "review", "establish",
    "comprehensive", "effective", "innovative", "strategic", "detailed",
    "structured", "seamless", "foundational", "realistic", "ongoing",
    "improved", "iterative", "motivated", "skilled", "specialized",
    "professional", "technical", "analytical", "operational", "financial",
    "organizational", "digital", "visual", "senior", "executive",
    "federal", "strong", "ready",
    "best", "laws", "regulations", "information", "content", "solutions",
    "structure", "detail", "identity", "platform", "behaviors", "visions",
    "objectives", "technology", "products", "projects", "responsibilities",
    "opportunities", "improvements", "consistency", "curiosity", "branding",
    "proficiency", "competency", "creation", "supports", "service",
    "adjudication", "suitability",
}
_CONNECTOR_RE = re.compile(r"\b(and|or|these|those|their|this|that|such|its|our|your|"
                           r"based|related|specific|including)\b", re.IGNORECASE)
_NOUN_GERUND_EXCEPTIONS = frozenset({
    "training", "planning", "consulting", "marketing", "engineering",
    "accounting", "staffing", "reporting", "budgeting", "coaching",
    "modeling", "networking", "recruiting", "manufacturing",
})


def filter_candidates(keywords: list[dict]) -> list[dict]:
    """Single words: whitelist or 3+ mentions. Multi-word (2-4): drop connector, verb-led and gerund-led
    fragments. Unicode dashes become ASCII so resume scoring matches either form."""
    filtered, seen = [], set()
    for kw in keywords:
        phrase = re.sub(r"[‑‒–—―]", "-", kw["keyword"].strip())
        phrase_lower = phrase.lower()
        if phrase_lower in seen or _FRAGMENT_PUNCT.search(phrase) or phrase[0].isdigit():
            continue
        words = phrase_lower.split()
        if len(words) > 4:
            continue
        if len(words) == 1:
            if phrase_lower in _SINGLE_WORD_NOISE:
                continue
            if phrase_lower not in _CERT_TOOL_WHITELIST and kw.get("count", 1) < 3:
                continue
        else:
            if _CONNECTOR_RE.search(phrase_lower) or words[0] in _VERB_STARTERS:
                continue
            w0 = words[0]
            if (w0.endswith("ing") and len(w0) > 5 and w0 not in _NOUN_GERUND_EXCEPTIONS
                    and w0 not in _CERT_TOOL_WHITELIST and w0 not in _VERB_STARTERS):
                continue
        filtered.append({**kw, "keyword": phrase} if kw["keyword"] != phrase else kw)
        seen.add(phrase_lower)
    return filtered


def extract_keywords(jd_text: str) -> list[dict]:
    return filter_candidates(process_jd(jd_text))
