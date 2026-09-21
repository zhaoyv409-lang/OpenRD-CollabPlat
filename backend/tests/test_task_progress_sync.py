import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.main import app
from app.models.demand import Demand
from app.models.task import Task, TaskProgress, TaskStage


TASK_ID = "B11-SYNC"


def _task() -> Task:
    return Task(
        id=TASK_ID,
        demand_id="REQ-B11",
        title="B11 阶段同步",
        description="验证阶段主表、历史、下一步计划和需求同步",
        status="in_progress",
        team_status="collaborating",
        stage=TaskStage.TEAM,
        owner_id="owner-b11",
        leader_id="leader-b11",
        is_deleted=0,
    )


def _demand() -> Demand:
    return Demand(
        id="REQ-B11",
        title="B11 关联需求",
        description="验证关联需求同步",
        status="converted",
        creator_id="requester-b11",
        linked_task_id=TASK_ID,
        progress=0,
        is_deleted=0,
    )


async def _operator() -> dict:
    return {"user_id": "operator-b11", "role": "operator", "jti": "b11"}


@pytest.fixture
async def progress_scene(db_session: AsyncSession) -> dict:
    task = _task()
    demand = _demand()
    db_session.add_all([task, demand])
    await db_session.commit()
    await db_session.refresh(task)
    return {"updated_at": task.updated_at}


def _version(value) -> str | None:
    return value.isoformat() if value else None


@pytest.mark.asyncio
async def test_stage_and_next_plan_persist_history_and_sync_linked_demand(
    client: AsyncClient,
    db_session: AsyncSession,
    progress_scene: dict,
):
    app.dependency_overrides[get_current_user] = _operator
    try:
        first = await client.post(
            f"/api/v1/tasks/{TASK_ID}/progress",
            json={
                "stage": "develop",
                "content": "完成第一阶段",
                "next_plan": "开始接口联调",
                "file_ids": [],
                "base_stage": "team",
                "expected_updated_at": _version(progress_scene["updated_at"]),
            },
        )
        assert first.status_code == 200, first.text

        detail = await client.get(f"/api/v1/tasks/{TASK_ID}")
        assert detail.status_code == 200
        assert detail.json()["data"]["stage"] == "develop"

        second = await client.post(
            f"/api/v1/tasks/{TASK_ID}/progress",
            json={
                "stage": "beta",
                "content": "完成接口联调",
                "next_plan": "组织内测反馈",
                "base_stage": "develop",
                "expected_updated_at": detail.json()["data"]["updated_at"],
            },
        )
        assert second.status_code == 200, second.text

        history = await client.get(f"/api/v1/tasks/{TASK_ID}/progress")
        assert history.status_code == 200, history.text
        items = history.json()["data"]["items"]
        assert [item["stage"] for item in items] == ["beta", "develop"]
        assert [item["next_plan"] for item in items] == ["组织内测反馈", "开始接口联调"]
        assert history.json()["data"]["total"] == 2
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    task = await db_session.get(Task, TASK_ID, populate_existing=True)
    demand = await db_session.get(Demand, "REQ-B11", populate_existing=True)
    assert task.stage == TaskStage.BETA
    assert demand.progress == 75


@pytest.mark.asyncio
async def test_stale_stage_update_is_rejected_without_side_effects(
    client: AsyncClient,
    db_session: AsyncSession,
    progress_scene: dict,
):
    app.dependency_overrides[get_current_user] = _operator
    stale_value = _version(progress_scene["updated_at"])
    try:
        accepted = await client.post(
            f"/api/v1/tasks/{TASK_ID}/progress",
            json={
                "stage": "develop",
                "base_stage": "team",
                "expected_updated_at": stale_value,
            },
        )
        assert accepted.status_code == 200, accepted.text

        conflict = await client.post(
            f"/api/v1/tasks/{TASK_ID}/progress",
            json={
                "stage": "beta",
                "content": "旧页面覆盖",
                "next_plan": "错误计划",
                "base_stage": "team",
                "expected_updated_at": stale_value,
            },
        )
        assert conflict.status_code == 409, conflict.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    rows = (
        await db_session.execute(
            select(TaskProgress)
            .where(TaskProgress.task_id == TASK_ID)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    task = await db_session.get(Task, TASK_ID, populate_existing=True)
    demand = await db_session.get(Demand, "REQ-B11", populate_existing=True)
    assert len(rows) == 1
    assert task.stage == TaskStage.DEVELOP
    assert demand.progress == 50


@pytest.mark.asyncio
async def test_expected_updated_at_field_is_required(client: AsyncClient, progress_scene: dict):
    app.dependency_overrides[get_current_user] = _operator
    try:
        response = await client.post(
            f"/api/v1/tasks/{TASK_ID}/progress",
            json={"stage": "develop", "base_stage": "team"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 422
