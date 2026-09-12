"""
一键灌数脚本：往 PostgreSQL 的 orders 表插入模拟订单（V5）。

    python scripts/seed_data.py

幂等：以 order_id 为主键做 upsert——已存在的订单按脚本内容更新，
不存在的插入。重复执行不会产生重复数据。

前置条件：.env 里配好 DATABASE_URL，PostgreSQL 服务已启动。
"""

import asyncio
import os
import sys

# 让脚本能从项目根目录 import db（脚本不在根目录，需要手动加 path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from db import AsyncSessionLocal, Order, ensure_tables  # noqa: E402

# 覆盖客服场景的主要分支：正常在途 / 配送中 / 已签收 / 待发货 / 物流异常 / 退款
SEED_ORDERS = [
    {
        "order_id": "12345",
        "user_id": "U1001",
        "status": "已发货",
        "logistics_status": "正常",
        "logistics_detail": "快件已到达【上海转运中心】，预计明日送达",
    },
    {
        "order_id": "9527",
        "user_id": "U1002",
        "status": "已发货",
        "logistics_status": "异常",
        "logistics_detail": "快件在【广州转运中心】滞留超过 48 小时，物流轨迹未更新",
    },
    {
        "order_id": "88888",
        "user_id": "U1001",
        "status": "待发货",
        "logistics_status": "无",
        "logistics_detail": "订单尚未出库，暂无物流信息",
    },
    {
        "order_id": "67890",
        "user_id": "U1003",
        "status": "配送中",
        "logistics_status": "正常",
        "logistics_detail": "快件正在派送中，快递员：张师傅 138****5678",
    },
    {
        "order_id": "24680",
        "user_id": "U1002",
        "status": "已签收",
        "logistics_status": "正常",
        "logistics_detail": "快件已签收，签收方式：本人前台代收",
    },
    {
        "order_id": "13579",
        "user_id": "U1003",
        "status": "已发货",
        "logistics_status": "异常",
        "logistics_detail": "快件外包装破损，转运中心已上报核实",
    },
    {
        "order_id": "77777",
        "user_id": "U1004",
        "status": "退款中",
        "logistics_status": "无",
        "logistics_detail": "订单已进入退款流程，原物流信息已归档",
    },
]


async def main():
    await ensure_tables()
    async with AsyncSessionLocal() as session:
        for fields in SEED_ORDERS:
            order = await session.get(Order, fields["order_id"])
            if order is None:
                session.add(Order(**fields))
                action = "插入"
            else:
                for k, v in fields.items():
                    setattr(order, k, v)
                action = "更新"
            print(f"  {action} 订单 {fields['order_id']}（{fields['status']}）")
        await session.commit()

        total = await session.scalar(select(func.count()).select_from(Order))
    print(f"完成：orders 表现有 {total} 条订单。")


if __name__ == "__main__":
    asyncio.run(main())
