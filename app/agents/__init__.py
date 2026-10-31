"""The AI agents. Each module is one role; prompts live in prompts/, models in config/agents.yaml.

document_analyst     page images -> transcription of scans + descriptions of figures/tables/equations
request_interpreter  a session message -> scope (which PDFs/pages/topics), needs, setting changes
learning_designer    selected blocks -> ordered segments with key points (+ optional basics segment)
script_writer        one segment -> spoken turns with source references
audience_editor      turns -> the same turns worded for the listener's age group
reviewer             script vs source -> approve or send back with issues
speech_director      approved turns -> delivery style + speech-safe wording
"""
