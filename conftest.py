"""
conftest.py — pytest（Python 测试框架）配置，提供共享 fixture（夹具/测试依赖）

client fixture 的作用：
  pytest 收集 test_ 开头的函数时，会按参数名去 conftest 里找同名 fixture。
  脚本原本的test_basic(client) 就是因为找不到 client fixture 才报
  "fixture 'client' not found"。
"""

import os
import sys

import pytest
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()


@pytest.fixture(scope="session")
def client():
    """整个测试会话共用一个 API 客户端（省去重复建连）"""
    if not API_KEY:
        pytest.skip(
            "未配置 DEEPSEEK_API_KEY（DeepSeek API 密钥），跳过需要联网的测试。"
            " 配置方法见 .env 文件。"
        )
    from openai import OpenAI

    return OpenAI(
        api_key=API_KEY,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )


@pytest.fixture(scope="session")
def model():
    """当前使用的模型名，供测试引用"""
    return os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")