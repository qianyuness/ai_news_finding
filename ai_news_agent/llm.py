from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from .filters import build_fallback_summary, extract_finance_info, extract_paper_info, infer_category, score_article
from .models import Article
from .utils import extract_json_objects, trim_text


VALID_CATEGORIES = {
    "ai_application",
    "ai_model",
    "ai_safety",
    "ai_investment",
    "research_paper",
}

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


class NewsAnalyzer:
    def __init__(self, config: dict[str, Any], logger, progress_callback=None) -> None:
        self.config = config
        self.logger = logger
        self.progress_callback = progress_callback
        llm_config = config.get("llm", {})
        self.enabled = bool(llm_config.get("enabled", True))
        self.provider = self._resolve_provider(llm_config)
        self.api_key_env, self._api_key = self._resolve_env_value(
            [
                "LLM_API_KEY",
                str(llm_config.get("api_key_env", "") or ""),
                "OPENAI_API_KEY",
                "KIMI_API_KEY",
                "MOONSHOT_API_KEY",
                "DASHSCOPE_API_KEY",
            ]
        )
        self.model = self._resolve_model(llm_config)
        self.api_url = self._resolve_api_url(llm_config)
        self.base_url = self._resolve_base_url(llm_config)
        self.temperature = self._resolve_float("LLM_TEMPERATURE", llm_config.get("temperature", 0.2))
        self.top_p = self._resolve_float("LLM_TOP_P", llm_config.get("top_p", 0.8))
        self.max_workers = self._resolve_int("LLM_MAX_WORKERS", llm_config.get("max_workers", 4), minimum=1)
        self.max_concurrency = self._resolve_int(
            "LLM_MAX_CONCURRENCY",
            llm_config.get("max_concurrency") or self._default_max_concurrency(),
            minimum=1,
        )
        self.max_concurrency = min(self.max_concurrency, self.max_workers)
        self.timeout_seconds = self._resolve_int("LLM_TIMEOUT_SECONDS", llm_config.get("timeout_seconds", 60), minimum=1)
        self.max_tokens = self._resolve_int("LLM_MAX_TOKENS", llm_config.get("max_tokens", 2048), minimum=0)
        self.max_retries = self._resolve_int("LLM_MAX_RETRIES", llm_config.get("max_retries", 3), minimum=0)
        self.requests_per_minute = self._resolve_int(
            "LLM_REQUESTS_PER_MINUTE",
            llm_config.get("requests_per_minute", 20),
            minimum=0,
        )
        self._request_interval_seconds = 60 / self.requests_per_minute if self.requests_per_minute > 0 else 0
        self._rate_lock = threading.Lock()
        self._next_request_at = 0.0
        self._force_temperature_one = False
        self._forced_top_p: float | None = None
        self.filtering = config.get("filtering", {})
        summary_cfg = config.get("summary", {})
        quality_cfg = config.get("quality", {})
        self.summary_min_chars = int(summary_cfg.get("min_chars", 100))
        self.summary_max_chars = int(summary_cfg.get("max_chars", 300))
        self.min_quality_score = int(quality_cfg.get("min_score", 68))
        self._llm_available = self.enabled and bool(self._api_key) and bool(self.model) and bool(self._completion_endpoint)

    @property
    def llm_available(self) -> bool:
        return self._llm_available

    @property
    def mode_label(self) -> str:
        if not self._llm_available:
            return "规则摘要"
        provider = self._infer_provider_label()
        return f"{provider} / {self.model}" if self.model else provider

    def analyze_articles(self, articles: list[Article]) -> list[Article]:
        if not articles:
            return []

        if not self._llm_available:
            self.logger.info("未检测到可用的大模型 API 配置，切换到规则摘要模式。")
            fallback_results: list[Article] = []
            total = max(len(articles), 1)
            for index, article in enumerate(articles, start=1):
                fallback_results.append(self._apply_fallback(article))
                self._report_progress(index, total, f"规则模式摘要处理中（{index}/{total}）...")
            return fallback_results

        results: list[Article] = []
        total = max(len(articles), 1)
        completed = 0
        worker_count = max(1, min(self.max_workers, self.max_concurrency, len(articles)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {executor.submit(self._analyze_single, article): article for article in articles}
            for future in as_completed(future_map):
                article = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("大模型分析失败，已回退到规则模式：%s | %s", article.url, exc)
                    results.append(self._apply_fallback(article))
                completed += 1
                self._report_progress(completed, total, f"大模型摘要与翻译处理中（{completed}/{total}）...")
        return results

    def _analyze_single(self, article: Article) -> Article:
        system_prompt = (
            "你是一名用于政府内部晨报的AI产业资讯分析助手。"
            "请基于给定资讯，输出严格的 JSON 对象，不要输出任何解释、markdown 或代码块。"
        )
        user_prompt = f"""
请阅读以下资讯，并用中文输出 JSON，字段必须完整：
{{
  "title_zh": "中文标题，若原标题已是中文可保持不变",
  "category": "只能是 ai_application / ai_model / ai_safety / ai_investment / research_paper 之一",
  "importance_score": 0-100 的整数，
  "quality_score": 0-100 的整数，
  "quality_reason": "一句中文说明，解释资讯是否值得进入日报",
  "summary": "{self.summary_min_chars}-{self.summary_max_chars}字中文摘要，适合内部简报，英文内容必须转成中文表述",
  "key_points": ["2-4条中文要点"],
  "tags": ["最多4个标签"],
  "finance_info": {{
    "company": "",
    "round": "",
    "amount": "",
    "investors": "",
    "business": ""
  }},
  "paper_info": {{
    "venue": "",
    "institution": "",
    "takeaway": ""
  }}
}}

资讯标题：{article.title}
来源：{article.source_name}
发布时间：{article.published_at.isoformat() if article.published_at else "未知"}
正文摘要：{trim_text(article.snippet, 500)}
正文内容：{trim_text(article.body_text, 4000)}
"""

        content = self._call_chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        payloads = extract_json_objects(content)
        payload = payloads[0] if payloads else {}
        if not payload:
            raise ValueError(f"无法解析大模型返回内容：{content[:200]}")

        article.title_zh = trim_text(payload.get("title_zh") or article.title, 80)
        if article.forced_category in VALID_CATEGORIES:
            article.category = article.forced_category
        else:
            category = str(payload.get("category", "")).strip()
            article.category = category if category in VALID_CATEGORIES else infer_category(article, self.filtering)
        fallback_summary, fallback_points = build_fallback_summary(
            article,
            min_chars=self.summary_min_chars,
            max_chars=self.summary_max_chars,
        )
        article.summary = trim_text(payload.get("summary") or fallback_summary, self.summary_max_chars + 20)
        article.key_points = self._normalize_list(payload.get("key_points")) or fallback_points
        article.tags = self._normalize_list(payload.get("tags"), max_items=4)
        article.finance_info = self._normalize_mapping(payload.get("finance_info"))
        article.paper_info = self._normalize_mapping(payload.get("paper_info"))
        article.metadata["quality_reason"] = trim_text(str(payload.get("quality_reason", "")).strip(), 100)

        raw_score = payload.get("importance_score", 0)
        try:
            article.importance_score = max(0.0, min(float(raw_score), 100.0))
        except (TypeError, ValueError):
            article.importance_score = score_article(article, self.filtering)

        try:
            quality_score = max(0, min(int(payload.get("quality_score", 0)), 100))
        except (TypeError, ValueError):
            quality_score = self._fallback_quality_score(article)
        article.metadata["quality_score"] = quality_score

        if article.category == "ai_investment" and not any(article.finance_info.values()):
            article.finance_info = extract_finance_info(article)
        if article.category == "research_paper" and not any(article.paper_info.values()):
            article.paper_info = extract_paper_info(article)
        return article

    def _call_chat_completion(self, messages: list[dict[str, str]]) -> str:
        endpoint = self._completion_endpoint
        if not endpoint:
            raise ValueError("未配置 LLM_BASE_URL 或 LLM_API_URL。")

        last_error = ""
        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            try:
                response = requests.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self._build_completion_payload(messages),
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(self._retry_delay_seconds(None, attempt, last_error))
                    continue
                raise RuntimeError(last_error) from exc

            if response.status_code < 400:
                return self._extract_message_content(response.json())

            error_text = response.text[:1000]
            last_error = f"HTTP {response.status_code}: {error_text[:500]}"

            if self._is_temperature_only_one_error(error_text):
                self._force_temperature_one = True
                continue

            forced_top_p = self._extract_forced_top_p(error_text)
            if forced_top_p is not None:
                self._forced_top_p = forced_top_p
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(self._retry_delay_seconds(response, attempt, error_text))
                    continue

            raise RuntimeError(last_error)

        raise RuntimeError(last_error or "大模型 API 请求失败。")

    def _build_completion_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 1 if self._force_temperature_one else self.temperature,
            "top_p": self._forced_top_p if self._forced_top_p is not None else self.top_p,
            "stream": False,
        }
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens
        return payload

    def _wait_for_rate_limit(self) -> None:
        if self._request_interval_seconds <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            wait_seconds = max(self._next_request_at - now, 0)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._next_request_at = time.monotonic() + self._request_interval_seconds

    def _retry_delay_seconds(self, response: requests.Response | None, attempt: int, error_text: str) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                return max(float(retry_after), 1)
            except ValueError:
                pass

        match = re.search(r"after\s+(\d+(?:\.\d+)?)\s+seconds?", error_text, flags=re.IGNORECASE)
        if match:
            return max(float(match.group(1)), 1)
        return min(2**attempt, 30)

    def _is_temperature_only_one_error(self, error_text: str) -> bool:
        lowered = error_text.casefold()
        return "temperature" in lowered and "only 1" in lowered

    def _extract_forced_top_p(self, error_text: str) -> float | None:
        match = re.search(r"top_p[^0-9]+only\s+(\d+(?:\.\d+)?)", error_text, flags=re.IGNORECASE)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    def _apply_fallback(self, article: Article) -> Article:
        summary, key_points = build_fallback_summary(
            article,
            min_chars=self.summary_min_chars,
            max_chars=self.summary_max_chars,
        )
        article.title_zh = article.title if article.locale.startswith("zh") else ""
        article.summary = summary
        article.key_points = key_points
        article.category = infer_category(article, self.filtering)
        article.importance_score = score_article(article, self.filtering)
        article.metadata["quality_score"] = self._fallback_quality_score(article)
        article.metadata["quality_reason"] = "规则模式下按正文长度、时效性和关键词命中进行质量估计。"
        if article.category == "ai_investment":
            article.finance_info = extract_finance_info(article)
        if article.category == "research_paper":
            article.paper_info = extract_paper_info(article)
        return article

    def _extract_message_content(self, response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")
                if isinstance(content, list):
                    return "".join(
                        str(item.get("text", "")) if isinstance(item, dict) else str(item)
                        for item in content
                    )
                return str(content)
            output_text = response.get("output_text")
            if output_text:
                return str(output_text)
        return str(response)

    @property
    def _completion_endpoint(self) -> str:
        if self.api_url:
            return self.api_url
        if not self.base_url:
            return ""
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def _resolve_provider(self, llm_config: dict[str, Any]) -> str:
        provider = os.getenv("LLM_PROVIDER") or str(llm_config.get("provider", "openai_compatible"))
        return provider.strip().casefold() or "openai_compatible"

    def _resolve_model(self, llm_config: dict[str, Any]) -> str:
        _, model = self._resolve_env_value(
            [
                "LLM_MODEL",
                str(llm_config.get("model_env", "") or ""),
                "OPENAI_MODEL",
                "KIMI_MODEL",
                "MOONSHOT_MODEL",
                "DASHSCOPE_MODEL",
            ]
        )
        return model or str(llm_config.get("model", "") or "").strip()

    def _resolve_api_url(self, llm_config: dict[str, Any]) -> str:
        _, api_url = self._resolve_env_value(
            [
                "LLM_API_URL",
                str(llm_config.get("api_url_env", "") or ""),
            ]
        )
        return api_url.rstrip("/") if api_url else str(llm_config.get("api_url", "") or "").strip().rstrip("/")

    def _resolve_base_url(self, llm_config: dict[str, Any]) -> str:
        _, base_url = self._resolve_env_value(
            [
                "LLM_BASE_URL",
                str(llm_config.get("base_url_env", "") or ""),
                "OPENAI_BASE_URL",
                "KIMI_BASE_URL",
                "MOONSHOT_BASE_URL",
                "DASHSCOPE_BASE_URL",
            ]
        )
        if base_url:
            return base_url.rstrip("/")

        config_base_url = str(llm_config.get("base_url", "") or "").strip()
        if config_base_url:
            return config_base_url.rstrip("/")

        if self.api_key_env == "OPENAI_API_KEY":
            return DEFAULT_BASE_URLS["openai"]
        if self.api_key_env in {"KIMI_API_KEY", "MOONSHOT_API_KEY"}:
            return DEFAULT_BASE_URLS["kimi"]
        if self.api_key_env == "DASHSCOPE_API_KEY":
            return DEFAULT_BASE_URLS["dashscope"]
        if self.provider in DEFAULT_BASE_URLS:
            return DEFAULT_BASE_URLS[self.provider]
        return ""

    def _resolve_env_value(self, names: list[str]) -> tuple[str, str]:
        seen: set[str] = set()
        for name in names:
            env_name = str(name or "").strip()
            if not env_name or env_name in seen:
                continue
            seen.add(env_name)
            value = os.getenv(env_name, "").strip()
            if value:
                return env_name, value
        return "", ""

    def _resolve_int(self, env_name: str, fallback: Any, *, minimum: int) -> int:
        raw_value = os.getenv(env_name, "").strip() or fallback
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            parsed = int(fallback or minimum)
        return max(parsed, minimum)

    def _resolve_float(self, env_name: str, fallback: Any) -> float:
        raw_value = os.getenv(env_name, "").strip() or fallback
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return float(fallback)

    def _default_max_concurrency(self) -> int:
        provider_hint = f"{self.provider} {self.base_url} {self.api_url}".casefold()
        if "moonshot" in provider_hint or "kimi" in provider_hint:
            return min(self.max_workers, 2)
        return self.max_workers

    def _infer_provider_label(self) -> str:
        base = (self.api_url or self.base_url or "").casefold()
        if "moonshot" in base:
            return "Kimi API"
        if "openai" in base:
            return "OpenAI API"
        if "dashscope" in base:
            return "Qwen API"
        if self.provider and self.provider != "openai_compatible":
            return f"{self.provider} API"
        return "自定义大模型 API"

    def _normalize_list(self, value: Any, max_items: int = 4) -> list[str]:
        if isinstance(value, list):
            return [trim_text(str(item).strip(), 60) for item in value if str(item).strip()][:max_items]
        return []

    def _normalize_mapping(self, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {str(key): trim_text(str(item).strip(), 60) for key, item in value.items() if str(item).strip()}

    def _fallback_quality_score(self, article: Article) -> int:
        score = 40
        if len(article.body_text) >= 500:
            score += 18
        elif len(article.body_text) >= 250:
            score += 10
        if article.published_at:
            score += 12
        if article.source_weight >= 1.2:
            score += 10
        elif article.source_weight >= 1.1:
            score += 6
        if len(article.title) >= 16:
            score += 4
        if article.category in {"ai_model", "ai_safety", "research_paper"}:
            score += 6
        return min(score, 100)

    def _report_progress(self, completed: int, total: int, message: str) -> None:
        if not self.progress_callback:
            return
        progress = 58 + int((completed / max(total, 1)) * 16)
        self.progress_callback(
            {
                "progress": progress,
                "stage": "analyzing",
                "message": message,
                "details": {"completed_articles": completed, "total_articles": total},
            }
        )
