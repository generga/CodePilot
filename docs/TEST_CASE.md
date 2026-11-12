# CodePilot 首次测试用例

## 用例信息

- 用例编号：`CP-001`
- 场景：修复用户注册邮箱为空时的边界异常
- 基线仓库：`D:\codepilot-demo-repo`
- 测试副本：`D:\codepilot-demo-repo-test-001`
- 风险等级：中等，需要人工审批补丁

## 控制台填写

打开：`http://127.0.0.1:8001/console`

先创建隔离测试副本：

```powershell
Copy-Item D:\codepilot-demo-repo D:\codepilot-demo-repo-test-001 -Recurse
```

仓库路径：

```text
D:\codepilot-demo-repo-test-001
```

问题描述：

```text
修复 user/service.py 中 normalize_email 在 email 为 None 时抛出 AttributeError 的问题。
当调用 normalize_email(None) 时，应抛出 ValueError，错误信息必须是 "email is required"。
保留现有的邮箱去空格和转小写行为，不要修改测试文件和其他文件。
完成修改后运行 pytest tests/test_user.py -q 验证。
```

执行 API Key：`dev-local`

审批 API Key：`review-local`

## 预期过程

1. 任务进入 `waiting_approval`。
2. Diff 只修改 `user/service.py`。
3. 审批前，`D:\codepilot-demo-repo\user\service.py` 不发生变化。
4. 点击“批准并继续”。
5. 任务进入 `completed`。
6. 测试结果为 `2 passed`。

## 预期代码变化

```python
if email is None:
    raise ValueError("email is required")
return email.strip().lower()
```

## 安全检查

- 审批前不能直接修改仓库。
- Diff 不能包含 `tests/test_user.py`。
- Agent 不应执行 pytest 以外的命令。
- 审批 API Key 不能使用普通 developer Key。

## 恢复基线

重复测试时重新从原始基线创建新的隔离副本，不要直接修改原始演示仓库：

```powershell
Copy-Item D:\codepilot-demo-repo D:\codepilot-demo-repo-test-002 -Recurse
```
