# agents.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Dict, Any
import json
from langchain_openai import ChatOpenAI

def build_llm(base_url: str, api_key: str, model: str, temperature: float = 0.2, timeout: int = 60) -> ChatOpenAI:
    return ChatOpenAI(
        openai_api_base=base_url,
        openai_api_key=api_key,
        model=model,
        temperature=temperature,
        timeout=timeout,
    )

class AgentA:
    def __init__(self, llm: ChatOpenAI, system_prompt: str, tools: Any, max_tool_calls: int = 2, logger=None):
        self.llm = llm
        self.system_prompt = system_prompt
        self.tools = tools
        self.max_tool_calls = max_tool_calls
        self.logger = logger

    def _log_prompt(self, tag: str, content: str):
        if self.logger and getattr(self.logger, "show_prompts", False):
            self.logger.log(tag, content.replace("\n"," ")[:2000])

    def run(self, *, file_path: str, file_content: str, codeql_query: str, rule_id_hint: str = "") -> Dict[str, Any]:
        # 固定顺序：知识库 -> 可疑片段选择
        plan = [
            {"name": "retrieve_vuln_kb", "args": {"rule_or_query": rule_id_hint or codeql_query}},
            {"name": "select_suspicious", "args": {
                "file_path": file_path,
                "file_content": file_content,
                "codeql_query": codeql_query,
                "context_window": 12,
                "max_snippets": 3
            }},
        ][: self.max_tool_calls]
        if self.logger:
            self.logger.log("AGENT_A", f"Planned tool calls: {plan}")

        tool_outputs: List[Dict[str, Any]] = []
        for call in plan:
            name = call["name"]
            args = call.get("args", {})
            if self.logger and getattr(self.logger, "show_tool_io", True):
                self.logger.log("TOOL-CALL", f"{name} args={args}")
            out = self.tools.call(name, **args)
            if self.logger and getattr(self.logger, "show_tool_io", True):
                self.logger.log("TOOL-RET", f"{name} -> keys={list(out.keys())}")
            tool_outputs.append(out)

        summary_prompt = f"""You are Agent A. Produce a compact brief for Red/Blue debate.

Inputs:
- File path: {file_path}
- Full file length: {len(file_content)} chars
- CodeQL query (raw):
{codeql_query}

Tool outputs (JSON):
{json.dumps(tool_outputs, ensure_ascii=False, indent=2)}

Write a brief that includes:
- Potential issue summary (from vuln KB if any)
- Most suspicious snippets (line ranges at a high level)
- Data/control flow angles to examine
- Quick remediation ideas
Limit to ~250-300 words.
"""
        self._log_prompt("LLM-PROMPT/A-SUM", summary_prompt)
        brief = self.llm.invoke([{"role":"system","content":self.system_prompt},
                                 {"role":"user","content":summary_prompt}]).content
        if self.logger:
            self.logger.log("AGENT_A", f"Brief len={len(brief)}")
        return {"plan": plan, "tool_outputs": tool_outputs, "brief": brief}

class AgentB:
    def __init__(self, llm: ChatOpenAI, system_prompt: str, logger=None):
        self.llm = llm
        self.system_prompt = system_prompt
        self.logger = logger

    def argue(self, transcript: List[Dict[str, str]], context: str) -> str:
        convo = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in transcript])
        prompt = f"""{self.system_prompt}

Agent A brief:
{context}

Debate so far:
{convo}

Your turn (Red Team): propose concrete exploit ideas, inputs, payloads, and why they work.
Keep it within 8 sentences."""
        if self.logger and getattr(self.logger, "show_prompts", False):
            self.logger.log("LLM-PROMPT/B", prompt.replace("\n"," "))
        out = self.llm.invoke([{"role":"system","content":self.system_prompt},
                               {"role":"user","content":prompt}]).content
        if self.logger:
            self.logger.log("AGENT_B", f"Argue len={len(out)}")
        return out

class AgentC:
    def __init__(self, llm: ChatOpenAI, system_prompt: str, logger=None):
        self.llm = llm
        self.system_prompt = system_prompt
        self.logger = logger

    def counter(self, transcript: List[Dict[str, str]], context: str) -> str:
        convo = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in transcript])
        prompt = f"""{self.system_prompt}

Agent A brief:
{context}

Debate so far:
{convo}

Your turn (Blue Team): refute false positives, point to safe APIs/sanitizers, and offer fixes.
Keep it within 8 sentences."""
        if self.logger and getattr(self.logger, "show_prompts", False):
            self.logger.log("LLM-PROMPT/C", prompt.replace("\n"," "))
        out = self.llm.invoke([{"role":"system","content":self.system_prompt},
                               {"role":"user","content":prompt}]).content
        if self.logger:
            self.logger.log("AGENT_C", f"Counter len={len(out)}")
        return out

class AgentJudge:
    def __init__(self, llm: ChatOpenAI, system_prompt: str, logger=None):
        self.llm = llm
        self.system_prompt = system_prompt
        self.logger = logger

    def final_json(self, user_context: Dict[str, Any], a_brief: str, debate_summary: str) -> str:
        prompt = f"""{self.system_prompt}

Context:
- File path: {user_context.get('file_path')}
- CodeQL rule hint: {user_context.get('rule_id_hint','')}
- CodeQL query (raw):
{user_context.get('codeql_query','')}

Agent A brief:
{a_brief}

Red/Blue debate transcript:
{debate_summary}

Output ONLY the JSON object, no extra text or markdown:
"""
        if self.logger and getattr(self.logger, "show_prompts", False):
            self.logger.log("LLM-PROMPT/JUDGE", prompt.replace("\n"," "))
        out = self.llm.invoke([{"role":"system","content":self.system_prompt},
                               {"role":"user","content":prompt}]).content
        if self.logger:
            self.logger.log("JUDGE", f"Raw verdict len={len(out)}")
        # 保证严格 JSON
        try:
            parsed = json.loads(out)
            return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            start = out.find("{"); end = out.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(out[start:end+1])
                    return json.dumps(parsed, ensure_ascii=False)
                except Exception:
                    pass
            fallback = {
                "verdict": "unknown",
                "reasons": ["Model did not produce valid JSON."],
                "evidence": [],
                "confidence": 0.0,
                "recommendations": []
            }
            return json.dumps(fallback, ensure_ascii=False)
