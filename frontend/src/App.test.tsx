import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App, { JobResult } from './App'
import { subscribeToJob, type Job, type SymbolLookup } from './api'

const emptyFundFlow = {
  today: { unit: null, main_net_inflow: null, main_visible_inflow: null, main_hidden_inflow: null, retail_inflow: null },
  three_day: { unit: null, main_net_inflow: null, main_visible_inflow: null, main_hidden_inflow: null, retail_inflow: null },
  five_day: { unit: null, main_net_inflow: null, main_visible_inflow: null, main_hidden_inflow: null, retail_inflow: null },
}

const task: Job = {
  public_id: 'public-job-1',
  symbol: '600938',
  include_long_capture: true,
  status: 'QUEUED',
  error_code: null,
  created_at: '2026-08-21T08:00:00+00:00',
  collected_at: null,
  captures: [
    { kind: 'LARGE_ORDER_NET', status: 'PENDING', url: null, expires_at: null },
    { kind: 'LARGE_ORDER_AMOUNT', status: 'PENDING', url: null, expires_at: null },
    { kind: 'RETAIL_COUNT', status: 'PENDING', url: null, expires_at: null },
  ],
  values: {
    stock_name: null,
    current_price: null,
    change_percent: null,
    turnover_rate: null,
    large_order_net: null,
    large_order_amount: null,
    retail_count: null,
    macdfs: null,
    main_fund_flow: emptyFundFlow,
  },
  long_capture: { status: 'PENDING', url: null, expires_at: null },
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function symbolLookup(symbol: string, name: string, market = '17'): SymbolLookup {
  return { symbol, name, market }
}

function deferredResponse() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>((next) => { resolve = next })
  return { promise, resolve }
}

describe('Level2 web UI', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    cleanup()
    window.history.replaceState({}, '', '/')
    window.localStorage.clear()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('submits a verified symbol without changing the current page URL', async () => {
    window.history.replaceState({}, '', '/capture?view=history#latest')
    const currentUrl = window.location.href
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(symbolLookup('600938', '中国海油')))
      .mockResolvedValueOnce(jsonResponse(task, 202))
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码'), '600938')
    await screen.findByText('中国海油（600938）')
    expect(screen.getByRole('heading', { name: '同花顺数据采集' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '生成整页长截图' })).not.toBeChecked()
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))

    await waitFor(() => expect(screen.getByText('已进入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/v1/symbols/600938', expect.objectContaining({
      credentials: 'include',
    }))
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/v1/jobs', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ symbol: '600938', include_long_capture: false }),
    }))
    expect(window.location.href).toBe(currentUrl)
  })

  it('submits a long-capture task when the switch is turned on', async () => {
    const dataOnlyTask: Job = {
      ...task,
      include_long_capture: true,
    }
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(symbolLookup('601872', '招商轮船')))
      .mockResolvedValueOnce(jsonResponse(dataOnlyTask, 202))
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码'), '601872')
    await screen.findByText('招商轮船（601872）')
    await user.click(screen.getByRole('checkbox', { name: '生成整页长截图' }))
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))

    await waitFor(() => expect(screen.getByText('已进入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenCalledWith('/api/v1/jobs', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ symbol: '601872', include_long_capture: true }),
    }))
  })

  it('removes the retired collection copy from the page', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: '同花顺数据采集' })).toBeInTheDocument()
    expect(screen.queryByText('采集 Level2 数据，结果逐条保留')).not.toBeInTheDocument()
    expect(screen.queryByText('新建采集')).not.toBeInTheDocument()
    expect(screen.queryByText('提交新的采集任务')).not.toBeInTheDocument()
    expect(screen.queryByText('输入 6 位股票代码，查询到股票名称后才可提交。')).not.toBeInTheDocument()
    expect(screen.queryByText('关闭后由 App 在后台请求并回传数据，不打开搜索框、不切换股票页面。')).not.toBeInTheDocument()
  })

  it('does not query or enable submit before all six digits are entered', async () => {
    const user = userEvent.setup()

    render(<App />)
    const submit = screen.getByRole('button', { name: '提交采集任务' })
    expect(submit).toBeDisabled()

    await user.type(screen.getByLabelText('股票代码'), '60014')

    expect(fetch).not.toHaveBeenCalled()
    expect(submit).toBeDisabled()
  })

  it('queries immediately on the sixth digit and enables submit only after success', async () => {
    const pending = deferredResponse()
    vi.mocked(fetch).mockReturnValueOnce(pending.promise)
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码'), '600143')

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      '/api/v1/symbols/600143',
      expect.objectContaining({ credentials: 'include', signal: expect.any(AbortSignal) }),
    ))
    expect(screen.getByText('正在查询股票…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交采集任务' })).toBeDisabled()

    pending.resolve(jsonResponse(symbolLookup('600143', '金发科技')))

    expect(await screen.findByText('金发科技（600143）')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交采集任务' })).toBeEnabled()
  })

  it.each([
    [404, '未找到该股票'],
    [503, '股票查询暂时不可用'],
  ])('keeps submit disabled when symbol lookup returns %s', async (status, message) => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ detail: message }, status))
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码'), '600142')

    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交采集任务' })).toBeDisabled()
  })

  it('invalidates the verified stock as soon as the code changes', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(symbolLookup('600143', '金发科技')))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码')
    await user.type(input, '600143')
    expect(await screen.findByText('金发科技（600143）')).toBeInTheDocument()

    await user.type(input, '{Backspace}')

    expect(screen.queryByText('金发科技（600143）')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交采集任务' })).toBeDisabled()
  })

  it('does not let an older lookup response replace the newest code', async () => {
    const first = deferredResponse()
    const second = deferredResponse()
    vi.mocked(fetch)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码')
    await user.type(input, '600143')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    await user.clear(input)
    await user.type(input, '600938')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))

    second.resolve(jsonResponse(symbolLookup('600938', '中国海油')))
    expect(await screen.findByText('中国海油（600938）')).toBeInTheDocument()
    first.resolve(jsonResponse(symbolLookup('600143', '金发科技')))

    await waitFor(() => expect(screen.queryByText('金发科技（600143）')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: '提交采集任务' })).toBeEnabled()
  })

  it('restores a public job from the URL after refresh', async () => {
    window.history.replaceState({}, '', '/?job=public-job-1')
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(task))

    render(<App />)

    await waitFor(() => expect(screen.getByText('已进入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenCalledWith('/api/v1/jobs/public-job-1', expect.anything())
  })

  it('adds one sign and the Chinese market direction color to directional values', () => {
    render(<JobResult task={{
      ...task,
      status: 'COMPLETED',
      collected_at: '2026-08-21T08:01:02+00:00',
      values: {
        stock_name: '招商轮船',
        current_price: '19.78',
        change_percent: '7.15%',
        turnover_rate: '2.40%',
        large_order_net: '-0.02',
        large_order_amount: '-2802.6万',
        retail_count: '21.23',
        macdfs: '+0.012',
        main_fund_flow: emptyFundFlow,
      },
    }} />)

    expect(within(screen.getByText('当前涨跌幅').closest('article')!).getByText('+7.15%')).toHaveClass('market-up')
    expect(within(screen.getByText('大单净量').closest('article')!).getByText('-0.02')).toHaveClass('market-down')
    expect(within(screen.getByText('大单金额').closest('article')!).getByText('-2802.6万')).toHaveClass('market-down')
    expect(within(screen.getByText('散户数量').closest('article')!).getByText('+21.23')).toHaveClass('market-up')
    expect(within(screen.getByText('MACDFS').closest('article')!).getByText('+0.012')).toHaveClass('market-up')
  })

  it('renders the three-period main fund flow comparison with dynamic units and signs', () => {
    render(<JobResult task={{
      ...task,
      status: 'COMPLETED',
      values: {
        ...task.values,
        main_fund_flow: {
          today: { unit: '亿元', main_net_inflow: '5.35', main_visible_inflow: '-0.28', main_hidden_inflow: '5.63', retail_inflow: '-5.35' },
          three_day: { unit: '亿元', main_net_inflow: '12.96', main_visible_inflow: '3.63', main_hidden_inflow: '9.34', retail_inflow: '-12.96' },
          five_day: { unit: '万元', main_net_inflow: null, main_visible_inflow: null, main_hidden_inflow: null, retail_inflow: null },
        },
      },
    }} />)

    expect(screen.getByRole('columnheader', { name: /当日 亿元/ })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: /3日 亿元/ })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: /5日 万元/ })).toBeInTheDocument()
    const netRow = screen.getByText('主力净流入').closest('tr')!
    expect(within(netRow).getByText('+5.35')).toHaveClass('market-up')
    expect(within(netRow).getByText('+12.96')).toHaveClass('market-up')
    expect(within(netRow).getByText('未识别')).toBeInTheDocument()
    const retailRow = screen.getByText('散户流入').closest('tr')!
    expect(within(retailRow).getByText('-5.35')).toHaveClass('market-down')
  })

  it('keeps zero, unparseable, missing, price, and turnover values neutral', () => {
    render(<JobResult task={{
      ...task,
      status: 'PARTIAL',
      collected_at: '2026-08-21T08:01:02+00:00',
      values: {
        stock_name: '测试股份',
        current_price: '19.78',
        change_percent: '0.00%',
        turnover_rate: '2.40%',
        large_order_net: '0',
        large_order_amount: '+0.0万',
        retail_count: '未知',
        macdfs: null,
        main_fund_flow: emptyFundFlow,
      },
    }} />)

    expect(within(screen.getByText('当前股价').closest('article')!).getByText('19.78')).toHaveClass('market-neutral')
    expect(within(screen.getByText('换手率').closest('article')!).getByText('2.40%')).toHaveClass('market-neutral')
    expect(within(screen.getByText('当前涨跌幅').closest('article')!).getByText('0.00%')).toHaveClass('market-neutral')
    expect(within(screen.getByText('大单净量').closest('article')!).getByText('0')).toHaveClass('market-neutral')
    expect(within(screen.getByText('大单金额').closest('article')!).getByText('0.0万')).toHaveClass('market-neutral')
    expect(within(screen.getByText('散户数量').closest('article')!).getByText('未知')).toHaveClass('market-neutral')
    expect(within(screen.getByText('MACDFS').closest('article')!).getByText('未识别')).not.toHaveClass('market-up', 'market-down', 'market-neutral')
  })

  it('marks only values that used the OCR fallback', () => {
    const ocrTask = {
      ...task,
      status: 'COMPLETED' as const,
      values: {
        ...task.values,
        large_order_net: '-0.02',
        large_order_amount: '-2802.6万',
      },
      value_sources: {
        stock_name: null,
        current_price: null,
        change_percent: null,
        turnover_rate: null,
        large_order_net: 'OCR' as const,
        large_order_amount: 'INTERFACE' as const,
        retail_count: null,
        macdfs: null,
        main_fund_flow: {
          today: { main_net_inflow: null, main_visible_inflow: null, main_hidden_inflow: null, retail_inflow: null },
          three_day: { main_net_inflow: null, main_visible_inflow: null, main_hidden_inflow: null, retail_inflow: null },
          five_day: { main_net_inflow: null, main_visible_inflow: null, main_hidden_inflow: null, retail_inflow: null },
        },
      },
    }

    render(<JobResult task={ocrTask} />)

    expect(within(screen.getByText('大单净量').closest('article')!).getByText('OCR识别')).toBeInTheDocument()
    expect(within(screen.getByText('大单金额').closest('article')!).queryByText('OCR识别')).not.toBeInTheDocument()
  })

  it('keeps the long capture collapsed until the user opens its drawer', async () => {
    const user = userEvent.setup()
    render(<App initialTask={{
      ...task,
      status: 'PARTIAL',
      error_code: 'VALUE_RECOGNITION_FAILED',
      collected_at: '2026-08-21T08:01:02+00:00',
      values: {
        stock_name: '招商轮船',
        current_price: '19.78',
        change_percent: '7.15%',
        turnover_rate: '2.40%',
        large_order_net: '-0.02',
        large_order_amount: '-2802.6万',
        retail_count: '21.23',
        macdfs: '+0.012',
        main_fund_flow: emptyFundFlow,
      },
      long_capture: { status: 'READY', url: '/api/v1/jobs/public-job-1/capture', expires_at: '2026-08-22T08:00:00+00:00' },
    }} />)

    expect(screen.getByText('长截图已生成，部分数据未读取')).toBeInTheDocument()
    expect(screen.getByText('-0.02')).toBeInTheDocument()
    expect(screen.getByText('+21.23')).toBeInTheDocument()
    expect(screen.getByText('招商轮船（600938）')).toBeInTheDocument()
    expect(screen.queryByText('招商轮船', { selector: 'h3' })).not.toBeInTheDocument()
    expect(screen.queryByText('600938', { selector: '.job-symbol' })).not.toBeInTheDocument()
    expect(screen.getByText('19.78')).toBeInTheDocument()
    expect(screen.getByText('+7.15%')).toBeInTheDocument()
    expect(screen.getByText('2.40%')).toBeInTheDocument()
    expect(screen.getByText('-2802.6万')).toBeInTheDocument()
    expect(screen.getByText('+0.012')).toBeInTheDocument()
    expect(screen.getByText('MACDFS')).toBeInTheDocument()
    expect(screen.getByText(/采集时间/)).toBeInTheDocument()
    expect(screen.getByText('大单净量')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: '600938 Level2 整页长截图' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '展开长截图' }))

    expect(screen.getByRole('img', { name: '600938 Level2 整页长截图' })).toHaveAttribute('src', '/api/v1/jobs/public-job-1/capture')
    expect(screen.getByRole('link', { name: '打开长截图' })).toHaveAttribute('href', '/api/v1/jobs/public-job-1/capture')
    expect(screen.getByRole('button', { name: '收起长截图' })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByText('截图已就绪')).not.toBeInTheDocument()
  })

  it('shows that a completed data-only task did not request a long capture', () => {
    render(<App initialTask={{
      ...task,
      include_long_capture: false,
      status: 'COMPLETED',
      collected_at: '2026-08-21T08:01:02+00:00',
      values: {
        stock_name: '招商轮船',
        current_price: '19.78',
        change_percent: '7.15%',
        turnover_rate: '2.40%',
        large_order_net: '-0.02',
        large_order_amount: '-2802.6万',
        retail_count: '21.23',
        macdfs: '+0.012',
        main_fund_flow: emptyFundFlow,
      },
      long_capture: { status: 'SKIPPED', url: null, expires_at: null },
    }} />)

    expect(screen.getByText('数据已就绪')).toBeInTheDocument()
    expect(screen.getByText('未请求长截图')).toBeInTheDocument()
    expect(screen.queryByText('整页长截图尚未生成。')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '展开长截图' })).not.toBeInTheDocument()
  })

  it('keeps earlier collection records when a new job is submitted', async () => {
    const secondTask = { ...task, public_id: 'public-job-2', symbol: '000001' }
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(symbolLookup('600938', '中国海油')))
      .mockResolvedValueOnce(jsonResponse(task, 202))
      .mockResolvedValueOnce(jsonResponse(symbolLookup('000001', '平安银行', '33')))
      .mockResolvedValueOnce(jsonResponse(secondTask, 202))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码')
    await user.type(input, '600938')
    await screen.findByText('中国海油（600938）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))
    await waitFor(() => expect(screen.getByText('任务 public-job-1')).toBeInTheDocument())

    await user.clear(input)
    await user.type(input, '000001')
    await screen.findByText('平安银行（000001）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))

    await waitFor(() => expect(screen.getByText('任务 public-job-2')).toBeInTheDocument())
    expect(screen.getByText('任务 public-job-1')).toBeInTheDocument()
    expect(screen.getByText('采集历史')).toBeInTheDocument()
  })

  it('restores collection history saved by this browser', async () => {
    const secondTask = { ...task, public_id: 'public-job-2', symbol: '000001' }
    window.localStorage.setItem('ths_level2_job_history', JSON.stringify(['public-job-2', 'public-job-1']))
    vi.mocked(fetch).mockImplementation((input) => {
      const url = String(input)
      return Promise.resolve(jsonResponse(url.endsWith('public-job-2') ? secondTask : task))
    })

    render(<App />)

    await waitFor(() => expect(screen.getByText('任务 public-job-2')).toBeInTheDocument())
    expect(screen.getByText('任务 public-job-1')).toBeInTheDocument()
  })

  it('clears only the collection history stored in this browser', async () => {
    window.localStorage.setItem('ths_level2_job_history', JSON.stringify(['public-job-1']))
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(task))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const user = userEvent.setup()

    render(<App />)
    await waitFor(() => expect(screen.getByText('任务 public-job-1')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: '清空本机记录' }))

    expect(screen.queryByText('任务 public-job-1')).not.toBeInTheDocument()
    expect(window.localStorage.getItem('ths_level2_job_history')).toBeNull()
    expect(screen.getByText('还没有采集记录')).toBeInTheDocument()
  })

  it('shows the current FIFO queue position while a task is waiting', () => {
    render(<App initialTask={{ ...task, queue_position: 2 }} />)

    expect(screen.getByText('当前排队位置：第 2 位')).toBeInTheDocument()
  })

  it('renders an expired job without capture actions', () => {
    render(<App initialTask={{ ...task, status: 'EXPIRED' }} />)

    expect(screen.getByText('结果已过期')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '打开截图' })).not.toBeInTheDocument()
  })
})

describe('job SSE subscription', () => {
  class FakeEventSource {
    static instance: FakeEventSource | undefined
    static instances: FakeEventSource[] = []
    closed = false
    onerror: ((event: Event) => void) | null = null
    private readonly listeners = new Map<string, (() => void)[]>()

    constructor(_url: string) {
      FakeEventSource.instance = this
      FakeEventSource.instances.push(this)
    }
    addEventListener(name: string, listener: () => void) {
      this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener])
    }
    close() { this.closed = true }
    emitStatus() { this.listeners.get('status')?.forEach((listener) => listener()) }
  }

  afterEach(() => {
    cleanup()
    FakeEventSource.instances = []
    vi.unstubAllGlobals()
  })

  it('keeps native EventSource reconnecting after a transient error', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const changed = vi.fn()
    const unsubscribe = subscribeToJob('public-job-1', changed)
    const stream = FakeEventSource.instance!

    stream.emitStatus()
    stream.onerror?.(new Event('error'))
    stream.emitStatus()

    expect(changed).toHaveBeenCalledTimes(2)
    expect(stream.closed).toBe(false)
    unsubscribe()
    expect(stream.closed).toBe(true)
  })

  it('keeps one SSE connection while an active task changes from QUEUED to RUNNING', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(jsonResponse(symbolLookup('600938', '中国海油')))
      .mockResolvedValueOnce(jsonResponse(task, 202))
      .mockResolvedValueOnce(jsonResponse({ ...task, status: 'RUNNING' })))
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码'), '600938')
    await screen.findByText('中国海油（600938）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    FakeEventSource.instances[0].emitStatus()
    await waitFor(() => expect(screen.getByText('正在采集')).toBeInTheDocument())
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0].closed).toBe(false)
  })
})
