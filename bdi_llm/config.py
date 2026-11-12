import os

def provider_name():
    return os.getenv("LLM_PROVIDER", "mock")

def max_attempts():
    return int(os.getenv("MAX_ATTEMPTS", "3"))
