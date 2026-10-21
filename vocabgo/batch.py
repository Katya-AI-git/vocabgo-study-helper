# -*- coding: utf-8 -*-
"""批量刷题筛选用的小工具。"""

from __future__ import annotations

from typing import Any, Iterable


def score_of(task: dict[str, Any]) -> float:
    """把服务端可能返回的字符串/空值分数统一成 float。"""
    try:
        return float(task.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def progress_of(task: dict[str, Any]) -> float:
    """把任务进度统一成 0~100 的 float。"""
    try:
        return float(task.get("progress") or 0)
    except (TypeError, ValueError):
        return 0.0


def task_type_of(task: dict[str, Any]) -> int:
    try:
        return int(task.get("task_type") or 0)
    except (TypeError, ValueError):
        return 0


def class_task_kind(task: dict[str, Any]) -> str:
    """班级任务内部类型：1=学习，2=测试。"""
    return "learning" if task_type_of(task) == 1 else "test" if task_type_of(task) == 2 else "unknown"


def class_task_is_repeatable(task: dict[str, Any]) -> bool:
    return class_task_kind(task) == "learning"


def rounds_for_task(
    kind: str,
    task: dict[str, Any],
    *,
    class_rounds: int,
    study_rounds: int,
) -> int:
    """按任务真实类型决定外层轮数。测试任务永远只执行一次。"""
    if kind == "class":
        return 1 if class_task_kind(task) == "test" else max(1, int(class_rounds))
    return max(1, int(study_rounds))


def is_pending(task: dict[str, Any], max_score: float) -> bool:
    """分数未到阈值就视为还需要继续处理。"""
    return score_of(task) < float(max_score)


def pending_class_tasks(
    records: Iterable[dict[str, Any]],
    max_score: float = 10,
    *,
    include_completed: bool = False,
) -> list[dict[str, Any]]:
    """筛出已开始且进度未满的班级任务。

    班级任务只按 progress 判断是否还会继续；任务得分是 0~100，
    不能和单词满分阈值 10 混用，否则会把进度 100% 的任务重新加入。
    task_id<=0 的未开始任务不能直接刷。
    """
    out: list[dict[str, Any]] = []
    for item in records:
        try:
            task_id = int(item.get("task_id") or 0)
        except (TypeError, ValueError):
            task_id = 0
        repeatable_completed = include_completed and class_task_is_repeatable(item)
        if task_id > 0 and (progress_of(item) < 100 or repeatable_completed):
            out.append(item)
    return out


def pending_study_tasks(records: Iterable[dict[str, Any]], max_score: float = 10) -> list[dict[str, Any]]:
    """筛出未得满分的自学任务；task_id=-1 可以交给 ensure_study_task 自动创建。

    自学任务可以被重复执行来提高得分，因此以任务得分 100 为完成线。
    """
    return [item for item in records if score_of(item) < 100]


def effective_rounds(kind: str, rounds: int) -> int:
    """班级任务由内部会话续答控制，外层练习轮数只用于自学任务。"""
    if kind == "class":
        return 1
    return max(1, int(rounds))
