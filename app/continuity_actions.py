"""Explicit, conservative operator reviews over an unchanged continuity view."""

import re
import uuid


KEYS = {"security_id", "item_id", "status", "reason", "revision_token"}
OPERATIONS = {"keep", "not_due", "cannot_determine", "withdrawn"}


def _item(projection, item_id):
    found = [item for item in projection["items"] if item["item_id"] == item_id]
    if len(found) != 1:
        raise ValueError("请从当前证券的研究关注项中选择一项")
    return found[0]


def _status(item, operation):
    if operation == "not_due" and item["window"]["state"] != "not_due" and item["effective"]["status"] != "not_due":
        raise ValueError("原关注项没有尚未结束的明确窗口，不能新造未到期判断")
    return item["effective"]["status"] if operation == "keep" else operation


def save_review(store, body):
    """Append one operator-confirmed review; never alter a frozen definition.

    The final token check happens inside the Lab writer lock, so a page and an
    independent command-line writer cannot both confirm the same stale view.
    """
    if not isinstance(body, dict) or set(body) != KEYS:
        raise ValueError("回顾只接受证券、关注项、状态、原因和当前版本标识")
    security_id = store.lab.identifier(body["security_id"], "security_id")
    item_id = store.lab.identifier(body["item_id"], "item_id")
    operation = body["status"]
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError("只支持保留原状态、未到期、无法判断或撤回")
    reason = body["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("回顾原因须为1至1000个有效字符")
    reason = reason.strip()
    token = body["revision_token"]
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("当前版本标识无效，请刷新研究室")
    projection = store.continuity_dashboard(security_id)
    if projection["revision_token"] != token:
        raise FileExistsError("研究档案已变化，请刷新后重新确认")
    original = _item(projection, item_id)
    status = _status(original, operation)
    current_use = "historical" if status == "withdrawn" or original["effective"]["current_use"] == "historical" else "needs_review"
    timestamp = store.lab.now()
    review_id = "REVIEW-UI-" + uuid.uuid4().hex
    folder = store.lab.inside(store.root, "_workspace", "platform", "review-preparation", uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    report = store.lab.inside(folder, "review.md")
    with report.open("x", encoding="utf-8", newline="\n") as output:
        output.write("# 研究关注项人工回顾\n\n")
        output.write(f"- 证券：{security_id}\n- 关注项：{item_id}\n")
        output.write(f"- 原始研究：{original['source']['result_id']}\n")
        output.write(f"- 原始冻结日期：{original['original']['frozen_at']}\n")
        output.write(f"- 本次回顾日期：{timestamp}\n- 本次操作：{operation}\n- 保留或更新后状态：{status}\n\n")
        output.write("## 原关注项\n\n" + original["original"]["text"] + "\n\n")
        output.write("## 人工回顾原因\n\n" + reason + "\n\n")
        output.write("本次没有新增外部证据，资料截止保持未知；不认定预测成功、不恢复当前可用资格，也不改变原判据或观察窗口。\n")
    request = {"security_id": security_id, "review_id": review_id, "generated_at": timestamp,
               "research_asof": timestamp, "data_asof": None, "evidence": [], "report_file": str(report),
               "updates": [{"item_id": item_id, "status": status, "reason": reason, "current_use": current_use}]}

    def check(catalog):
        matches = [(record, item) for record in catalog["results"] for item in record["items"]
                   if item["item_id"] == item_id]
        if (len(matches) != 1 or matches[0][0]["security"]["security_id"] != security_id
                or matches[0][0]["result_id"] != original["source"]["result_id"]):
            raise FileExistsError("关注项来源已变化，请刷新后重新确认")
        fresh = store.continuity_dashboard(security_id)
        if fresh["revision_token"] != token:
            raise FileExistsError("研究档案已变化，请刷新后重新确认")
        if _status(_item(fresh, item_id), operation) != status:
            raise FileExistsError("关注项状态已变化，请刷新后重新确认")

    committed = store.lab.review(store.root, request, check=check)
    result = {"saved": True, "review_id": review_id, "report_id": "review:" + review_id,
              "catalog_updated": committed.get("catalog_updated", False), "continuity": None}
    if committed.get("warning"):
        result["warning"] = "回顾已保存，档案索引需要重建；请刷新后核查。"
    try:
        result["continuity"] = store.continuity_dashboard(security_id)
    except Exception:
        # A committed append must not be presented as failed: retrying would
        # create another formal review rather than repairing the read failure.
        result["warning"] = "回顾已保存，但看板暂未重新读取成功；请刷新，不要重复提交。"
    return result
