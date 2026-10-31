"""The one place where agents talk to a model. Every agent goes through run_json()."""
from ..llm import openrouter
from . import registry


def call_model(spec, messages):
    """Send messages to the agent's model and return the raw reply text. Tests replace this function."""
    return openrouter.chat(spec.model, messages, json_mode=True, temperature=spec.temperature, max_tokens=spec.max_tokens)


def run_json(agent, user, *, variant=None, images=None, **prompt_values):
    """Run an agent and return its JSON reply as a dict.

    agent:  name in config/agents.yaml
    user:   the user message (the material for this call)
    images: optional list of PNG bytes (only for agents with vision: true)
    prompt_values: fill {{placeholders}} in the agent's prompt file
    """
    spec = registry.spec(agent)
    system = registry.system_prompt(agent, variant, **prompt_values)
    content = user
    if images:
        if not spec.vision:
            raise ValueError(f"Agent '{agent}' is not marked vision: true in config/agents.yaml")
        content = [{"type": "text", "text": user}] + [openrouter.image_part(b) for b in images]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    reply = call_model(spec, messages)
    try:
        return openrouter.extract_json(reply)
    except ValueError:
        retry = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": "That was not valid JSON. Reply again with ONLY the JSON object."},
        ]
        return openrouter.extract_json(call_model(spec, retry))
