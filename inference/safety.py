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
    "kill myself", "end my life", "suicide", "die", "hurt myself", "self-harm", 
    "cut myself", "take my life", "don't want to live", "ending it all", "better off dead"
]
CRISIS_LABELS = ["suicidal", "self-harm", "depressed"]

def is_crisis(message, label, score):
    """
    Checks if a message indicates a crisis using both detected label and keywords.
    """
    # Keyword check (High priority override)
    message_lower = message.lower()
    if any(word in message_lower for word in CRISIS_KEYWORDS):
        return True
    
    # Label check with tuned threshold (increased to 0.7 for label-based detection)
    if label.lower() in CRISIS_LABELS and score > 0.7:
        return True
        
    return False
