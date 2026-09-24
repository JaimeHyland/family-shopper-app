import asyncio

from channels_redis.core import RedisChannelLayer


async def test():
    layer = RedisChannelLayer(
        hosts=[("127.0.0.1", 6379)]
    )

    await layer.group_add(
        "test-group",
        "test-channel",
    )

    print("GROUP ADD OK")

    await layer.group_send(
        "test-group",
        {
            "type": "test.message",
            "message": "hello",
        },
    )

    print("GROUP SEND OK")

    message = await layer.receive("test-channel")

    print("RECEIVED:", message)


asyncio.run(test())
