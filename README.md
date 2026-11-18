# 想法簿

一个本地运行的想法群聊。机器人可以分别回答、彼此回应，也可以引用你此前主动总结过的会话。

## 启动

需要 Python 3.11 以上和 [uv](https://docs.astral.sh/uv/)。

```sh
uv sync
uv run uvicorn thought_app.app:app --host 127.0.0.1 --port 8000
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。首次启动会预置 DeepSeek V4.1 Flash（`deepseek-flash`）和忧郁、客观、积极、浪漫四个机器人。

预置 DeepSeek API 从本地 `.env` 读取 `DEEPSEEK_API_KEY`。也可以在运维后台填写密钥，适用于 DeepSeek 和采用 OpenAI Chat Completions 格式的接口。为兼容接口填服务的基础地址，比如 `https://api.example.com/v1`。

## 使用

- **聊天**：新建会话，选择机器人。普通发送各自回复，点击「让他们讨论」时先独立发言，再互相回应一次。
- **记忆**：打开一个会话，选择总结模型并生成总结。可以修改内容、重新生成，以及设置是否供其他会话引用。覆盖范围外出现新消息时，总结会标为待更新。
- **跨会话引用**：下一次聊天会按标题和关键词挑选最多三条已开启引用的总结。聊天里可以打开相关会话，带原话引用的回复可以跳到对应消息。
- **运维**：增改删 API、模型、人格模板和机器人，或用已配置的模型测试 API 连通性。

数据保存在 `.data/thoughts.sqlite3`。可以用 `THOUGHT_DB` 指定其他文件，方便隔离测试。聊天和生成总结时，所选外部模型会收到完成任务所需的会话内容和相关记忆片段。

## 回归测试

```sh
uv run python -m unittest discover -s tests -v
```

测试使用临时数据库和模拟模型调用。
