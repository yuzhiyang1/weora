"""运行对外错误不携带数据库、模型凭据或任务正文。"""


class RunError(Exception):
    """运行用例的可预期错误。"""

    code = "RUN_ERROR"
    message = "运行操作未完成"


class RunNotFoundError(RunError):
    """不存在和无权访问使用同一错误，避免泄漏私有运行存在性。"""

    code = "RUN_NOT_FOUND"
    message = "运行不存在或无权访问"


class RunVersionNotFoundError(RunError):
    """只允许选择当前工作空间内的明确发布版本。"""

    code = "RUN_VERSION_NOT_FOUND"
    message = "Agent 发布版本不存在或无权访问"


class RunIdempotencyConflictError(RunError):
    """同一成员的提交键不能复用于不同目标或版本。"""

    code = "RUN_IDEMPOTENCY_CONFLICT"
    message = "该提交键已用于其他请求，请为新任务使用新的提交键"


class RunCancellationConflictError(RunError):
    """本切片仅支持取消未调度运行，不假装已经停止执行中的外部请求。"""

    code = "RUN_CANCELLATION_CONFLICT"
    message = "当前运行不处于可取消的排队状态"


class RunSubmissionRaceError(Exception):
    """持久化唯一键竞争，由应用层回读已成功提交的原请求。"""


class RunProjectSelectionError(RunError):
    code = "RUN_PROJECT_SELECTION_INVALID"
    message = "项目或仓库不可用，请刷新选择；不能选择其他项目或已停用的仓库"


class RunContinuationLimitError(RunError):
    """持久跟进有界排队，不让同一目标无限堆积正文。"""

    code = "TASK_INPUT_LIMIT"
    message = "该任务的待处理消息已达到上限，请等待现有消息处理"


class RunSteeringUnavailableError(RunError):
    code = "RUN_STEERING_UNAVAILABLE"
    message = "这次运行已结束或暂时无法接收指令，可选择在后续运行处理"


class RunModelSelectionError(RunError):
    """内部协调配置只能选择已启用提供方的模型标识，不能注入新提供方或凭据。"""

    code = "RUN_MODEL_SELECTION_INVALID"
    message = "模型选择无效或当前执行内核未启用"
