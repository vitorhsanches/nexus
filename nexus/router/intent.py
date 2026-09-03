import re
import unicodedata


EXECUTION = "EXECUTION"
ANALYSIS = "ANALYSIS"
QUESTION = "QUESTION"
PLANNING = "PLANNING"

VALID_INTENTS = (EXECUTION, ANALYSIS, QUESTION, PLANNING)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _has_word(text: str, term: str) -> bool:
    return re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", text) is not None


QUESTION_KEYWORDS = (
    "explain how",
    "explain what",
    "explain why",
    "what is",
    "what are",
    "how does",
    "how do",
    "compare",
    "difference between",
    "why does",
    "why is",
)

PLANNING_KEYWORDS = (
    "roadmap",
    "design architecture",
    "design the architecture",
    "plan the",
    "planning for",
    "future implementation",
    "propose an architecture",
    "architecture proposal",
)

ANALYSIS_KEYWORDS = (
    "analyze",
    "analyse",
    "analysis",
    "review the",
    "review project",
    "review architecture",
    "explain the risks",
    "risks",
    "suggest improvements",
    "assess",
    "audit",
    "evaluate",
    "investigate",
)

EXECUTION_KEYWORDS = (
    "fix",
    "implement",
    "create",
    "add",
    "update",
    "modify",
    "refactor",
    "remove",
    "delete",
    "build",
    "write",
    "change",
    "patch",
    "resolve",
    "automate",
    "automation",
    "correct",
    "corrija",
    "implemente",
    "crie",
    "adicione",
    "modifique",
    "corrigir",
)


def classify_intent(text: str) -> str:
    """Deterministically classify a natural-language request into one of the
    four Nexus intent categories.

    Classification is purely keyword/structure based; no external model call
    is used, so the result is stable and reproducible for a given input.

    Priority order (most to least specific):
    1. PLANNING - future implementation / roadmap requests.
    2. EXECUTION - requests that require code/repository changes. This
       always takes priority over question wording (question words,
       question marks, "how"/"why"/"what") when an execution signal is
       present.
    3. QUESTION - direct-answer requests.
    4. ANALYSIS - default for everything else (review/explain/assess).
    """
    if not text or not text.strip():
        return ANALYSIS

    normalized = _normalize(text)

    for keyword in PLANNING_KEYWORDS:
        if keyword in normalized:
            return PLANNING

    for keyword in EXECUTION_KEYWORDS:
        if _has_word(normalized, keyword):
            return EXECUTION

    for keyword in QUESTION_KEYWORDS:
        if keyword in normalized:
            return QUESTION

    if normalized.rstrip().endswith("?"):
        return QUESTION

    for keyword in ANALYSIS_KEYWORDS:
        if keyword in normalized:
            return ANALYSIS

    return ANALYSIS
