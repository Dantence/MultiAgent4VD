# main.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import yaml
import json
from typing import Dict, Any, List
from datetime import datetime

from agents import build_llm, AgentA, AgentB, AgentC, AgentJudge
from tools import FakeToolset
from sarif_utils import load_sarif, iter_results, extract_result_files, safe_read_text

# -------- 基于 print 的极简日志 --------
class PLogger:
    def __init__(self, log_cfg: Dict[str, Any]):
        self.enabled = bool(log_cfg.get("enabled", True))
        self.show_prompts = bool(log_cfg.get("show_prompts", False))
        self.show_tool_io = bool(log_cfg.get("show_tool_io", True))
        self.show_debate_rounds = bool(log_cfg.get("show_debate_rounds", True))
        self.timestamp = bool(log_cfg.get("timestamp", True))
        self.time_format = log_cfg.get("time_format", "%H:%M:%S")
        self.prefix = log_cfg.get("prefix", "[VulnAgents]")

    def _ts(self) -> str:
        if not self.timestamp:
            return ""
        return datetime.now().strftime(self.time_format)

    def log(self, section: str, msg: str):
        if not self.enabled:
            return
        ts = self._ts()
        if ts:
            print(f"{self.prefix} {ts} [{section}] {msg}")
        else:
            print(f"{self.prefix} [{section}] {msg}")

def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def _get_query_for_rule(cfg: Dict[str, Any], rule_id: str) -> str:
    cq = cfg.get("codeql", {})
    rule_map = cq.get("rule_query_map", {}) or {}
    if rule_id in rule_map and rule_map[rule_id]:
        return rule_map[rule_id]
    return cq.get("default_query_text", "")

def run_single_finding(*, cfg: Dict[str, Any], logger: PLogger,
                       file_path: str, file_content: str, codeql_query: str, rule_id_hint: str) -> Dict[str, Any]:
    logger.log("INIT", "Init tools & LLM")
    tools = FakeToolset(cfg["tools"], logger=logger)
    llm = build_llm(
        base_url=cfg["llm"]["base_url"],
        api_key=cfg["llm"]["api_key"],
        model=cfg["llm"]["model"],
        temperature=cfg["llm"].get("temperature", 0.2),
        timeout=cfg["llm"].get("timeout", 60),
    )
    agent_a = AgentA(llm, cfg["prompts"]["agent_a_system"], tools,
                     max_tool_calls=cfg["app"]["max_tool_calls"], logger=logger)
    agent_b = AgentB(llm, cfg["prompts"]["agent_b_system"], logger=logger)
    agent_c = AgentC(llm, cfg["prompts"]["agent_c_system"], logger=logger)
    judge   = AgentJudge(llm, cfg["prompts"]["judge_system"], logger=logger)

    # A：简报
    logger.log("AGENT_A", f"Analyze file: {file_path} (rule={rule_id_hint})")
    a_result = agent_a.run(
        file_path=file_path, file_content=file_content,
        codeql_query=codeql_query, rule_id_hint=rule_id_hint
    )
    brief = a_result["brief"]
    logger.log("AGENT_A", "Brief prepared.")

    # B/C：辩论
    transcript: List[Dict[str, str]] = []
    rounds = int(cfg["app"]["debate_rounds"])
    logger.log("DEBATE", f"Start debate rounds={rounds}")
    for i in range(rounds):
        logger.log("DEBATE", f"Round {i+1} - Red")
        b_msg = agent_b.argue(transcript, brief)
        transcript.append({"role": "b", "content": b_msg})
        if logger.show_debate_rounds:
            logger.log("AGENT_B", b_msg.replace("\n"," "))

        logger.log("DEBATE", f"Round {i+1} - Blue")
        c_msg = agent_c.counter(transcript, brief)
        transcript.append({"role": "c", "content": c_msg})
        if logger.show_debate_rounds:
            logger.log("AGENT_C", c_msg.replace("\n"," "))

    logger.log("DEBATE", "Summarize debate")
    debate_summary = "\n\n".join([f"{m['role'].upper()}: {m['content']}" for m in transcript])

    # Judge：最终 JSON
    logger.log("JUDGE", "Produce final JSON")
    verdict_json = judge.final_json(
        user_context={"file_path": file_path, "rule_id_hint": rule_id_hint, "codeql_query": codeql_query},
        a_brief=brief, debate_summary=debate_summary
    )
    try:
        parsed = json.loads(verdict_json)
    except Exception:
        parsed = {"verdict": "unknown", "raw": verdict_json}
    logger.log("RESULT", f"Done for file={file_path}")
    return parsed

def run_from_sarif_only(cfg: Dict[str, Any], sarif_path: str) -> List[Dict[str, Any]]:
    """
    你只提供 SARIF 文件；定位与读取源码由本函数完成（方法2）：
      - 解析 result -> 定位文件 -> 读取整份源码
      - 选择 CodeQL 查询文本（ruleId 命中映射或默认值）
      - 触发多智能体流程，输出每个 finding 的 JSON 裁决
    """
    logger = PLogger(cfg.get("logging", {}))
    logger.log("SARIF", f"Loading: {sarif_path}")
    sarif = load_sarif(sarif_path)
    results = list(iter_results(sarif))
    logger.log("SARIF", f"Results count={len(results)}")

    outputs: List[Dict[str, Any]] = []
    for idx, res in enumerate(results, 1):
        rule_id = res.get("ruleId") or (res.get("rule", {}) or {}).get("id") or ""
        files = extract_result_files(res, cfg)
        if not files:
            logger.log("SARIF", f"[{idx}] No resolvable file paths, skip.")
            outputs.append({"index": idx, "ruleId": rule_id, "error": "no-file"})
            continue

        # 选择 CodeQL 查询文本
        codeql_query = _get_query_for_rule(cfg, rule_id)

        for fp in files:
            text = safe_read_text(
                fp,
                encoding=cfg["app"].get("default_encoding","utf-8"),
                max_chars=cfg["app"].get("max_file_chars", 120000)
            )
            if not text:
                logger.log("SARIF", f"[{idx}] Cannot read file: {fp}")
                outputs.append({"index": idx, "ruleId": rule_id, "file": fp, "error": "unreadable"})
                continue

            q = f"Analyze potential issue based on CodeQL rule '{rule_id}' and the given full file content."
            verdict = run_single_finding(
                cfg=cfg, logger=logger,
                file_path=fp, file_content=text, codeql_query=codeql_query, rule_id_hint=rule_id
            )
            outputs.append({"index": idx, "ruleId": rule_id, "file": fp, "verdict": verdict})
    return outputs

if __name__ == "__main__":
    # 使用方式：仅提供 SARIF 文件路径，其他定位逻辑系统内置
    cfg = load_config("config.yaml")
    sarif_path = "path/to/result.sarif"  # ← 修改为你的 SARIF 文件
    results = run_from_sarif_only(cfg, sarif_path)
    print("\n" + "="*30 + " ALL VERDICTS " + "="*30)
    print(json.dumps(results, ensure_ascii=False, indent=2))
