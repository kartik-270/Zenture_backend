CRISIS_RESPONSE = (
    "I'm very concerned to hear you're feeling this way. Please know that you are not alone and professional support is available 24/7. "
    "If you are in India, please consider reaching out to these trusted organizations immediately:\n"
    "- **KIRAN Mental Health Helpline**: 1800-599-0019\n"
    "- **Vandrevala Foundation**: 1860-2662-345 / 9999666555\n"
    "- **AASRA (Crisis & Suicide)**: 9820466726\n"
    "- **iCall (TISS)**: 022-25521111 (Mon-Sat, 8am-10pm)\n\n"
    "Your life is precious and Zenture Wellness is deeply committed to supporting you. Please reach out to one of these services now, or use the 'Book a Counselor Session' option to speak with our certified professionals."
)

CRISIS_KEYWORDS = [
    "kill myself", "end my life", "suicide", "suicidal", "die", "hurt myself", "self-harm",
    "cut myself", "take my life", "don't want to live", "ending it all", "better off dead",
    "want to die", "no reason to live"
]
CRISIS_LABELS = ["suicidal", "self-harm"]

# If a message contains these academic/everyday words, do NOT flag via label alone
# (keyword check above still catches true crisis phrases)
ACADEMIC_CONTEXT_WORDS = [
    "study", "studies", "exam", "exams", "test", "college", "university", "homework",
    "assignment", "marks", "grade", "school", "project", "syllabus", "lecture"
]

def is_crisis(message, label, score):
    """
    Checks if a message indicates a crisis using both detected label and keywords.
    """
    message_lower = message.lower()

    # 1. Keyword check — always takes priority regardless of label
    if any(word in message_lower for word in CRISIS_KEYWORDS):
        return True

    # 2. Label check — only when confidence is very high AND no academic context
    has_academic_context = any(w in message_lower for w in ACADEMIC_CONTEXT_WORDS)
    if not has_academic_context and label.lower() in CRISIS_LABELS and score >= 0.88:
        return True

    return False
