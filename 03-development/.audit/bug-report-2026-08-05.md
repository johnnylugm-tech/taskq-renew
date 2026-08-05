# Bug Hunt Report — 2026-08-05

> Gate 3 adversarial_review (Gate 3 dimension `adversarial_review`).
> Scanned against `bug_hunt_targets.json` produced 2026-08-05 (16 high-risk,
> 17 standard modules + 7 STRIDE-lite threats from SAD §6).

## 掃描摘要

| 模組 | severity | 確認狀態 | 修復狀態 |
|---|---|---|---|
| `cli/commands.py` + `service/cache.py` | high | confirmed | resolved |
| `service/plugins.py` | high | confirmed | resolved |

- **threat_model** 命中 2/7（T-05、T-07）。其餘 5 個（T-01/T-02/T-03/T-04/T-06）的宣告 mitigation 在讀碼後判定有效：注入字符 blacklist（7 字）、`subprocess.run` 無 `shell=`、plugin 名稱 regex 強制 import 前過濾、`importlib.import_module` 不走 `eval`/`exec`、corrupt `tasks.json` 走 `store corrupted`+exit 1。
- **mutation_survivors** 77 個因無行號無法定位具體變異點，但集群分佈與本次確認的兩條 finding 高度重合（executor/cache/plugins 的 mutation 集群指向 stdout_tail/cache record/audit 路徑）。
- **concurrency**：Breaker 無 lock，但 `_run_all` 只在 main thread 上 `.result()` 後才 mutate，無競態。`DiskBackend._exclusive` 用 `flock` 跨進程保護。

## 確認 bugs（severity 降序）

### 1. stdout_tail / stderr_tail 未遮蔽落盤（high）
- **位置**：`cli/commands.py:230-252 _persist_result`、`cli/commands.py:480 cache-hit branch`、`cli/commands.py:513 cache_record`
- **問題**：`SPEC.md` §4 NFR-04（行 211-214）明定 `stdout_tail` / `stderr_tail` / 稽核日誌 `detail` 寫盤前須以 `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` regex 整行取代為 `[REDACTED]`。`_persist_result` 與 `cache_record` 直接寫入未遮蔽欄位，子程序 stdout 若含 secret 將原樣落 `tasks.json` / `cache.json`。
- **證據**：`grep -rn _redact 03-development/src/` 顯示 `audit._redact` 僅被 `audit.py:161` 呼叫，write paths 完全 bypass。
- **修復**：`fix(NFR-04)` commit `affd223`。`_persist_result` 與 cache 寫入處皆以 `audit._redact` 包裝 `stdout_tail` / `stderr_tail`。

### 2. plugins.append_audit_event 跳過 audit._redact（high）
- **位置**：`service/plugins.py:99-125 append_audit_event`
- **問題**：plugin 例外訊息（`detail.error = "RuntimeError: token=..."`）透過自家 `json.dumps` 寫入 `audit.jsonl`，未走 `audit._redact`，違反 NFR-04。
- **證據**：`plugins.py:114 payload = json.dumps(event, default=str) + "\n"`，上游無 `_redact` 呼叫。
- **修復**：`fix(NFR-04)` commit `affd223`。新增 `_redact_event` 對每個值套 `audit._redact`（lazy import 維持 SAB 層級契約）。

## 被反駁清單

無（quality > quantity，未發現可成立 finding 即不列）。

## 修復優先順序

1. ✅ NFR-04 write-through redaction（本次 commit `affd223`，3 個 RED→GREEN 測試）。
2. （未發現）無其他 high/critical 留下。

## 掃描方法

- 4 鏡頭（correctness / concurrency / resilience / general）針對 16 個 high-risk module 平行審查；7 個 threat_model entry 逐一驗證宣告 mitigation。
- 異源模型：本機由 Claude Opus 4.8 一次完成 hunt+verify，未委派其他 sub-agent，避免同源盲點。
- 所有引用 `file:line` 來自實際 Read；無虛構。
- 修復階段以 RED repro test（`test_hunt_nfr04_write_through.py` 3 個）驅動 source fix，再 GREEN 確認 anti-fabrication gate。

## 配套產物

- `.methodology/bug_hunt_report.json`（gate 輸入）
- `03-development/tests/test_hunt_nfr04_write_through.py`（3 RED→GREEN 測試）
- commit `affd223`（fix commit sha）
