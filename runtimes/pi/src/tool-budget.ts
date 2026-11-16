/** 旧 SDK 会话的回合预算；业务额度耗尽后仍可持久化部分结果并交给新 Run 续接。 */
export class ToolBudget {
  private calls = 0;
  private closingCalls = 0;
  private denied = 0;
  exhausted = false;
  partialSummary: string | undefined;

  get stop(): boolean { return this.partialSummary !== undefined || this.denied >= 2 || this.closingCalls > 4; }

  private error(): Record<string, unknown> {
    return {
      ok: false, error: { code: 'TOOL_BUDGET_EXCEEDED' }, continuation_required: true,
      instruction: '本轮工具预算已用尽，原始任务仍须完整完成。请用 orchestrator_report 提交 state=partial、'
        + '实际证据 summary、completed_input_ids 和非空 remaining_work；平台会在新 Run 中续接。不得改报 ready。',
    };
  }

  before(name: string, args: unknown): Record<string, unknown> | undefined {
    const closing = ['orchestrator_report', 'orchestrator_ack', 'orchestrator_activity_cancel'].includes(name);
    const value = args && typeof args === 'object' ? args as Record<string, unknown> : {};
    if (closing && (this.calls >= 64 || this.exhausted)) {
      if (++this.closingCalls > 4 || (this.exhausted && value.state === 'ready')) return this.error();
      return;
    }
    if (this.calls >= 64) {
      this.exhausted = true;
      this.denied++;
      return this.error();
    }
    this.calls++;
  }

  after(name: string, args: unknown, result: Record<string, unknown>): Record<string, unknown> {
    const error = result.error && typeof result.error === 'object' ? result.error as Record<string, unknown> : {};
    if (error.code === 'TOOL_BUDGET_EXCEEDED') this.exhausted = true;
    const value = args && typeof args === 'object' ? args as Record<string, unknown> : {};
    if (name === 'orchestrator_report' && value.state === 'partial' && result.ok === true
        && (this.exhausted || this.calls >= 64) && typeof value.summary === 'string') {
      this.partialSummary = value.summary;
    }
    if (this.exhausted) return { ...result, runtime_budget: this.error() };
    if (this.calls >= 64) return { ...result, runtime_budget: {
      remaining: 0,
      instruction: '本轮业务工具额度已用完。目标全部完成时可正常交付；有未完成事项时报告 partial 以续接，不能缩小原目标。',
    } };
    return result;
  }
}
