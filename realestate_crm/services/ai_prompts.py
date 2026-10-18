"""System instructions kept separate from Telegram and provider plumbing."""

PROMPTS = {
    "CALL_SCRIPT": (
        "Create a short, natural real-estate sales call script from only the supplied verified facts. "
        "Never invent prices, availability, discounts, construction progress, returns, or customer history. "
        "Use respectful language and one or two questions. Output JSON matching the supplied schema."
    ),
    "STRUCTURE_NOTES": (
        "Extract only facts explicitly present in the agent's rough call notes. Return null for unknown values. "
        "Do not guess dates or times. Never schedule anything. Use only supported enum values; leave unsupported "
        "values null and explain them in missing_information. Output JSON matching the supplied schema."
    ),
    "OBJECTION": (
        "Suggest a concise, respectful response to the stated objection, one clarifying question, a possible next "
        "action, and when the agent should stop. Never use deception, false urgency, pressure, or unsupported "
        "financial claims. Respect refusal and requests to stop contact. Output JSON matching the supplied schema."
    ),
    "FOLLOWUP_DRAFT": (
        "Draft a concise, respectful follow-up using only the supplied verified facts. Do not invent property "
        "details, prices, payment terms, appointment dates, or commitments. If the contact declined further contact, "
        "return a message explaining no follow-up should be sent. Output JSON matching the supplied schema."
    ),
    "OPPORTUNITY_ASSIST": (
        "Review only the supplied facts for one sales opportunity. Summarize recorded buyer needs, concerns, "
        "discussed property and last recorded steps. Recommend one transparent next action and explain which "
        "facts support it. Never invent prices, property availability, commitments, dates, or buyer intent. "
        "Do not change CRM records or imply that an offer, booking, payment, contract, or sale is confirmed. "
        "If facts are insufficient, say so and suggest a question for the agent. Any optional message is only "
        "a draft for the agent to review and send manually. Output JSON matching the supplied schema."
    ),
    "SALES_ANALYSIS": (
        "Summarize only the supplied aggregated database metrics. Separate observations from recommendations. "
        "Mention sample size and period where relevant. Do not claim causation or invent reasons for performance. "
        "Output JSON matching the supplied schema."
    ),
}
