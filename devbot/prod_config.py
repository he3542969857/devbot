"""Config override for production."""
import os
os.environ["DEVBOT_LLM_PROVIDER"] = "openai"
os.environ["DEVBOT_LLM_BASE_URL"] = "https://api.siliconflow.cn/v1"
os.environ["DEVBOT_LLM_API_KEY"] = "YOUR_SILICONFLOW_API_KEY"
