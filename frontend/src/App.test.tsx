import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

function completedTabTask(publicId: string, symbol: string, name: string, price: string): Job {
  return {
    ...task,
    public_id: publicId,
    symbol,
    status: 'COMPLETED',
    values: {
      ...task.values,
      stock_name: name,
      current_price: price,
    },
  }
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
    await user.type(screen.getByLabelText('股票代码或名称'), '600938')
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
    await user.type(screen.getByLabelText('股票代码或名称'), '601872')
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

    await user.type(screen.getByLabelText('股票代码或名称'), '60014')

    expect(fetch).not.toHaveBeenCalled()
    expect(submit).toBeDisabled()
  })

  it('queries immediately on the sixth digit and enables submit only after success', async () => {
    const pending = deferredResponse()
    vi.mocked(fetch).mockReturnValueOnce(pending.promise)
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码或名称'), '600143')

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

  it('debounces a stock name query and shows App-internal suggestions', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse([
      { symbol: '688027', name: '国盾量子', market: '17', market_label: '科创' },
    ]))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')
    await user.type(input, '国盾')

    expect(fetch).not.toHaveBeenCalled()
    const option = await screen.findByRole('option', { name: /国盾量子.*688027.*科创/ })
    expect(option).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/symbols?query=%E5%9B%BD%E7%9B%BE&limit=8',
      expect.objectContaining({ credentials: 'include', signal: expect.any(AbortSignal) }),
    )
    expect(screen.getByRole('button', { name: '提交采集任务' })).toBeDisabled()
  })

  it('exactly confirms a selected suggestion before submitting its six-digit code', async () => {
    const selectedTask = { ...task, symbol: '688027' }
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse([
        { symbol: '688027', name: '国盾量子', market: '17', market_label: '科创' },
      ]))
      .mockResolvedValueOnce(jsonResponse(symbolLookup('688027', '国盾量子')))
      .mockResolvedValueOnce(jsonResponse(selectedTask, 202))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')
    await user.type(input, '国盾')
    await user.click(await screen.findByRole('option', { name: /国盾量子.*688027.*科创/ }))

    expect(await screen.findByText('国盾量子（688027）')).toBeInTheDocument()
    expect(input).toHaveValue('688027')
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/v1/symbols/688027', expect.objectContaining({
      signal: expect.any(AbortSignal),
    }))

    await user.click(screen.getByRole('button', { name: '提交采集任务' }))
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/v1/jobs', expect.objectContaining({
      body: JSON.stringify({ symbol: '688027', include_long_capture: false }),
    }))
  })

  it('supports keyboard selection and Escape for the suggestion list', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse([
        { symbol: '688027', name: '国盾量子', market: '17', market_label: '科创' },
        { symbol: '688981', name: '中芯国际', market: '17', market_label: '科创' },
      ]))
      .mockResolvedValueOnce(jsonResponse(symbolLookup('688027', '国盾量子')))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')
    await user.type(input, '国盾')
    await screen.findByRole('option', { name: /国盾量子/ })
    await user.keyboard('{ArrowDown}{Enter}')

    expect(await screen.findByText('国盾量子（688027）')).toBeInTheDocument()

    await user.clear(input)
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse([
      { symbol: '688981', name: '中芯国际', market: '17', market_label: '科创' },
    ]))
    await user.type(input, '中芯')
    await screen.findByRole('option', { name: /中芯国际/ })
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('option', { name: /中芯国际/ })).not.toBeInTheDocument()
  })

  it.each([
    [[], '没有匹配的股票'],
    [{ detail: 'symbol search temporarily unavailable' }, '股票候选查询暂时不可用', 503],
  ])('shows a distinct suggestion result state', async (body, message, status = 200) => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(body, status))
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码或名称'), '国盾')

    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交采集任务' })).toBeDisabled()
  })

  it('does not let an older suggestion response replace a newer query', async () => {
    const first = deferredResponse()
    const second = deferredResponse()
    vi.mocked(fetch)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')
    await user.type(input, '国盾')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1), { timeout: 1000 })
    await user.clear(input)
    await user.type(input, '中芯')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2), { timeout: 1000 })

    second.resolve(jsonResponse([
      { symbol: '688981', name: '中芯国际', market: '17', market_label: '科创' },
    ]))
    expect(await screen.findByRole('option', { name: /中芯国际/ })).toBeInTheDocument()

    first.resolve(jsonResponse([
      { symbol: '688027', name: '国盾量子', market: '17', market_label: '科创' },
    ]))
    await waitFor(() => expect(screen.queryByRole('option', { name: /国盾量子/ })).not.toBeInTheDocument())
  })

  it('waits for IME composition to finish before searching by name', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse([
      { symbol: '688027', name: '国盾量子', market: '17', market_label: '科创' },
    ]))
    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')

    fireEvent.compositionStart(input)
    fireEvent.change(input, { target: { value: '国盾' } })
    await new Promise((resolve) => window.setTimeout(resolve, 350))
    expect(fetch).not.toHaveBeenCalled()

    fireEvent.compositionEnd(input, { data: '国盾' })
    expect(await screen.findByRole('option', { name: /国盾量子/ })).toBeInTheDocument()
  })

  it.each([
    [404, '未找到该股票'],
    [503, '股票查询暂时不可用'],
  ])('keeps submit disabled when symbol lookup returns %s', async (status, message) => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ detail: message }, status))
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码或名称'), '600142')

    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交采集任务' })).toBeDisabled()
  })

  it('invalidates the verified stock as soon as the code changes', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(symbolLookup('600143', '金发科技')))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')
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
    const input = screen.getByLabelText('股票代码或名称')
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

  it('converts every main fund flow period to rounded hundred-million values', () => {
    render(<JobResult task={{
      ...task,
      status: 'COMPLETED',
      values: {
        ...task.values,
        main_fund_flow: {
          today: { unit: '亿元', main_net_inflow: '5.355', main_visible_inflow: '-0.28', main_hidden_inflow: '5.63', retail_inflow: '-5.355' },
          three_day: { unit: '万元', main_net_inflow: '12350', main_visible_inflow: '-12350', main_hidden_inflow: '50', retail_inflow: '-12350' },
          five_day: { unit: '万元', main_net_inflow: null, main_visible_inflow: null, main_hidden_inflow: null, retail_inflow: null },
        },
      },
    }} />)

    expect(screen.getByText('统一单位：亿元')).toBeInTheDocument()
    expect(screen.queryByText('万元')).not.toBeInTheDocument()
    const netRow = screen.getByText('主力净流入').closest('tr')!
    expect(within(netRow).getByText('+5.36')).toHaveClass('market-up')
    expect(within(netRow).getByText('+1.24')).toHaveClass('market-up')
    expect(within(netRow).getByText('未识别')).toBeInTheDocument()
    const visibleRow = screen.getByText('主力明盘').closest('tr')!
    expect(within(visibleRow).getByText('-1.24')).toHaveClass('market-down')
    const hiddenRow = screen.getByText('主力暗盘').closest('tr')!
    expect(within(hiddenRow).getByText('+0.01')).toHaveClass('market-up')
    const retailRow = screen.getByText('散户流入').closest('tr')!
    expect(within(retailRow).getByText('-1.24')).toHaveClass('market-down')
  })

  it('places main fund flow after MACDFS and the intraday curves last', () => {
    render(<JobResult task={{
      ...task,
      status: 'COMPLETED',
      values: {
        ...task.values,
        intraday_series: {
          large_order_net: { unit: null, points: [{ time: '09:30', value: '-0.02' }] },
          large_order_amount: { unit: '万', points: [] },
          retail_count: { unit: null, points: [] },
        },
      },
    }} />)

    const macdfs = screen.getByText('MACDFS').closest('article')!
    const fundFlow = screen.getByRole('heading', { name: '主力流向' }).closest('section')!
    const intraday = screen.getByRole('heading', { name: '当日分时' }).closest('section')!

    expect(macdfs.compareDocumentPosition(fundFlow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(fundFlow.compareDocumentPosition(intraday) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('renders three separate intraday charts and lets the keyboard inspect earlier points', async () => {
    const user = userEvent.setup()
    render(<JobResult task={{
      ...task,
      status: 'COMPLETED',
      values: {
        ...task.values,
        intraday_series: {
          large_order_net: {
            unit: null,
            points: [
              { time: '09:30', value: '-0.03' },
              { time: '09:31', value: '-0.02' },
            ],
          },
          large_order_amount: {
            unit: '万',
            points: [
              { time: '09:30', value: '-3397.0' },
              { time: '09:31', value: '-2802.6' },
            ],
          },
          retail_count: {
            unit: null,
            points: [
              { time: '09:30', value: '21.75' },
              { time: '09:31', value: '21.23' },
            ],
          },
        },
      },
    }} />)

    expect(screen.queryByRole('img', { name: '大单净量当日分时图' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '展开曲线' }))

    const netChart = screen.getByRole('img', { name: '大单净量当日分时图' })
    expect(screen.getByRole('img', { name: '大单金额当日分时图' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '散户数量当日分时图' })).toBeInTheDocument()
    expect(within(netChart).getAllByText('09:30', { selector: '.chart-axis-label' })).toHaveLength(1)
    expect(within(netChart.closest('figure')!).getByText('09:31', { selector: 'time' })).toBeInTheDocument()
    expect(within(netChart.closest('figure')!).getByText('-0.02', { selector: 'strong' })).toBeInTheDocument()

    netChart.focus()
    await user.keyboard('{ArrowLeft}')

    expect(within(netChart.closest('figure')!).getByText('09:30', { selector: 'time' })).toBeInTheDocument()
    expect(within(netChart.closest('figure')!).getByText('-0.03', { selector: 'strong' })).toBeInTheDocument()
  })

  it('collapses the intraday curves again after they are expanded', async () => {
    const user = userEvent.setup()
    render(<JobResult task={{
      ...task,
      status: 'COMPLETED',
      values: {
        ...task.values,
        intraday_series: {
          large_order_net: { unit: null, points: [{ time: '09:30', value: '-0.03' }] },
          large_order_amount: { unit: '万', points: [] },
          retail_count: { unit: null, points: [] },
        },
      },
    }} />)

    const toggle = screen.getByRole('button', { name: '展开曲线' })
    await user.click(toggle)
    expect(screen.getByRole('img', { name: '大单净量当日分时图' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '收起曲线' }))

    expect(screen.queryByRole('img', { name: '大单净量当日分时图' })).not.toBeInTheDocument()
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

  it('keeps scalar data visible when only the long capture has expired', () => {
    render(<JobResult task={{
      ...task,
      status: 'COMPLETED',
      values: {
        ...task.values,
        stock_name: '中国海油',
        current_price: '25.48',
      },
      long_capture: { status: 'EXPIRED', url: null, expires_at: null },
    }} />)

    expect(screen.getByText('中国海油（600938）')).toBeInTheDocument()
    expect(screen.getByText('25.48')).toBeInTheDocument()
    expect(screen.getByText('长截图已过期，指标数据仍保留')).toBeInTheDocument()
  })

  it('keeps earlier stocks as tabs when a new job is submitted', async () => {
    const secondTask = { ...task, public_id: 'public-job-2', symbol: '000001' }
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(symbolLookup('600938', '中国海油')))
      .mockResolvedValueOnce(jsonResponse(task, 202))
      .mockResolvedValueOnce(jsonResponse(symbolLookup('000001', '平安银行', '33')))
      .mockResolvedValueOnce(jsonResponse(secondTask, 202))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')
    await user.type(input, '600938')
    await screen.findByText('中国海油（600938）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))
    await waitFor(() => expect(screen.getByText('任务 public-job-1')).toBeInTheDocument())

    await user.clear(input)
    await user.type(input, '000001')
    await screen.findByText('平安银行（000001）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))

    await waitFor(() => expect(screen.getByText('任务 public-job-2')).toBeInTheDocument())
    expect(screen.queryByText('任务 public-job-1')).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /中国海油/ })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /平安银行/ })).toBeInTheDocument()
    expect(screen.getByText('股票页签')).toBeInTheDocument()
  })

  it('selects an earlier stock, refreshes it, and moves its tab to the top', async () => {
    const firstTask = { ...task, status: 'COMPLETED' as const }
    const secondTask = { ...task, public_id: 'public-job-2', symbol: '000001', status: 'COMPLETED' as const }
    const retriedTask = { ...firstTask, status: 'QUEUED' as const, error_code: null }
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(symbolLookup('600938', '中国海油')))
      .mockResolvedValueOnce(jsonResponse(firstTask, 202))
      .mockResolvedValueOnce(jsonResponse(symbolLookup('000001', '平安银行', '33')))
      .mockResolvedValueOnce(jsonResponse(secondTask, 202))
      .mockResolvedValueOnce(jsonResponse(retriedTask, 202))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')
    await user.type(input, '600938')
    await screen.findByText('中国海油（600938）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))
    await waitFor(() => expect(screen.getByText('任务 public-job-1')).toBeInTheDocument())

    await user.clear(input)
    await user.type(input, '000001')
    await screen.findByText('平安银行（000001）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))
    await waitFor(() => expect(screen.getByText('任务 public-job-2')).toBeInTheDocument())

    await user.click(screen.getByRole('tab', { name: /中国海油/ }))

    await waitFor(() => expect(screen.getByText('已进入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenLastCalledWith('/api/v1/jobs/public-job-1/retry', expect.objectContaining({ method: 'POST' }))
    expect(document.querySelectorAll('.job-result')).toHaveLength(1)
    expect(screen.getByText('任务 public-job-1')).toBeInTheDocument()
    expect(screen.getAllByRole('tab')[0]).toHaveAccessibleName(/中国海油/)
  })

  it('submits the same stock through the existing task id instead of adding a history row', async () => {
    const refreshedTask = { ...task, status: 'QUEUED' as const, error_code: null }
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(symbolLookup('600938', '中国海油')))
      .mockResolvedValueOnce(jsonResponse(task, 202))
      .mockResolvedValueOnce(jsonResponse(symbolLookup('600938', '中国海油')))
      .mockResolvedValueOnce(jsonResponse(refreshedTask, 202))
    const user = userEvent.setup()

    render(<App />)
    const input = screen.getByLabelText('股票代码或名称')
    await user.type(input, '600938')
    await screen.findByText('中国海油（600938）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))
    await waitFor(() => expect(screen.getByText('任务 public-job-1')).toBeInTheDocument())

    await user.type(input, '600938')
    await screen.findByText('中国海油（600938）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))

    await waitFor(() => expect(screen.getAllByText('任务 public-job-1')).toHaveLength(1))
    expect(document.querySelectorAll('.job-result')).toHaveLength(1)
  })

  it('repeatedly clicking the selected stock tab triggers a fresh update each time', async () => {
    const completedTask = { ...task, status: 'COMPLETED' as const, values: { ...task.values, stock_name: '中国海油' } }
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse({ ...completedTask, status: 'QUEUED' }, 202))
      .mockResolvedValueOnce(jsonResponse({ ...completedTask, status: 'QUEUED' }, 202))
    const user = userEvent.setup()

    render(<App initialTask={completedTask} />)
    const tab = screen.getByRole('tab', { name: /中国海油/ })
    await user.click(tab)
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    await user.click(tab)

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/v1/jobs/public-job-1/retry', expect.objectContaining({ method: 'POST' }))
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/v1/jobs/public-job-1/retry', expect.objectContaining({ method: 'POST' }))
  })

  it('shows only the active stock result and updates when another tab is selected', async () => {
    const first = { ...task, status: 'COMPLETED' as const, values: { ...task.values, stock_name: '中国海油', current_price: '25.48' } }
    const second = { ...task, public_id: 'public-job-2', symbol: '000001', status: 'COMPLETED' as const, values: { ...task.values, stock_name: '平安银行', current_price: '10.88' } }
    window.localStorage.setItem('ths_level2_stock_tabs_v2', JSON.stringify([
      { public_id: first.public_id, symbol: first.symbol, name: '中国海油' },
      { public_id: second.public_id, symbol: second.symbol, name: '平安银行' },
    ]))
    window.localStorage.setItem('ths_level2_active_stock_tab', first.public_id)
    vi.mocked(fetch).mockImplementation((input) => {
      const url = String(input)
      if (url.endsWith('/retry')) return Promise.resolve(jsonResponse({ ...second, status: 'QUEUED' }, 202))
      return Promise.resolve(jsonResponse(url.includes('public-job-2') ? second : first))
    })
    const user = userEvent.setup()

    render(<App />)
    await screen.findByText('25.48')
    expect(screen.queryByText('10.88')).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: /平安银行/ }))

    await screen.findByText('10.88')
    expect(screen.queryByText('25.48')).not.toBeInTheDocument()
    expect(document.querySelectorAll('.job-result')).toHaveLength(1)
  })

  it('migrates legacy browser history to one persistent tab per stock', async () => {
    const canonical = { ...task, public_id: 'canonical-job', status: 'COMPLETED' as const, values: { ...task.values, stock_name: '中国海油' } }
    window.localStorage.setItem('ths_level2_job_history', JSON.stringify(['old-job', 'canonical-job']))
    vi.mocked(fetch).mockResolvedValue(jsonResponse(canonical))

    render(<App />)

    await waitFor(() => expect(screen.getAllByRole('tab', { name: /中国海油/ })).toHaveLength(1))
    expect(JSON.parse(window.localStorage.getItem('ths_level2_stock_tabs_v2')!)).toEqual([
      { public_id: 'canonical-job', symbol: '600938', name: '中国海油' },
    ])
    expect(window.localStorage.getItem('ths_level2_job_history')).toBeNull()
  })

  it('provides a searchable all-stocks picker when more than five tabs exist', async () => {
    const storedTabs = Array.from({ length: 6 }, (_, index) => ({
      public_id: `public-job-${index + 1}`,
      symbol: `60093${index}`,
      name: `测试股票${index + 1}`,
    }))
    window.localStorage.setItem('ths_level2_stock_tabs_v2', JSON.stringify(storedTabs))
    vi.mocked(fetch).mockImplementation((input) => {
      const id = String(input).split('/').at(-1)!
      const index = Number(id.split('-').at(-1)) - 1
      return Promise.resolve(jsonResponse({
        ...task,
        public_id: id,
        symbol: storedTabs[index].symbol,
        status: 'COMPLETED',
        values: { ...task.values, stock_name: storedTabs[index].name },
      }))
    })
    const user = userEvent.setup()

    render(<App />)
    const allStocks = await screen.findByRole('button', { name: '全部股票 6' })
    await user.click(allStocks)
    await user.type(screen.getByRole('searchbox', { name: '搜索股票页签' }), '测试股票6')

    const picker = document.getElementById('all-stocks-picker')!
    expect(within(picker).getByRole('button', { name: /测试股票6/ })).toBeInTheDocument()
    expect(within(picker).queryByRole('button', { name: /测试股票1/ })).not.toBeInTheDocument()
  })

  it('bounds persistent stock tabs to the client-side event-stream limit', async () => {
    const storedTabs = Array.from({ length: 51 }, (_, index) => ({
      public_id: `persistent-${index}`,
      symbol: String(600000 + index),
      name: `永久股票${index}`,
    }))
    window.localStorage.setItem('ths_level2_stock_tabs_v2', JSON.stringify(storedTabs))
    vi.mocked(fetch).mockImplementation((input) => {
      const id = String(input).split('/').at(-1)!
      const index = Number(id.split('-').at(-1))
      return Promise.resolve(jsonResponse({
        ...task,
        public_id: id,
        symbol: storedTabs[index].symbol,
        status: 'COMPLETED',
        values: { ...task.values, stock_name: storedTabs[index].name },
      }))
    })

    render(<App />)

    await screen.findByRole('button', { name: '全部股票 50' })
    expect(JSON.parse(window.localStorage.getItem('ths_level2_stock_tabs_v2')!)).toHaveLength(50)
  })

  it('asks for confirmation and can cancel browser-only tab deletion', async () => {
    const first = completedTabTask('first', '600001', '第一股票', '10.01')
    const second = completedTabTask('second', '600002', '第二股票', '10.02')
    window.localStorage.setItem('ths_level2_stock_tabs_v2', JSON.stringify([
      { public_id: first.public_id, symbol: first.symbol, name: '第一股票' },
      { public_id: second.public_id, symbol: second.symbol, name: '第二股票' },
    ]))
    vi.mocked(fetch).mockImplementation((input) => Promise.resolve(
      jsonResponse(String(input).endsWith('/second') ? second : first),
    ))
    const user = userEvent.setup()

    render(<App />)
    await screen.findByRole('tab', { name: /第一股票/ })
    await user.click(screen.getByRole('button', { name: '删除 第一股票（600001）页签' }))

    expect(screen.getByRole('dialog', { name: '删除股票页签' })).toBeInTheDocument()
    expect(screen.getByText('确定从当前浏览器删除 第一股票（600001）吗？')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '取消' }))

    expect(screen.queryByRole('dialog', { name: '删除股票页签' })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /第一股票/ })).toBeInTheDocument()
    expect(JSON.parse(window.localStorage.getItem('ths_level2_stock_tabs_v2')!)).toHaveLength(2)
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('deletes the active tab locally and activates its right neighbor without refreshing', async () => {
    const left = completedTabTask('left', '600001', '左侧股票', '10.01')
    const middle = completedTabTask('middle', '600002', '中间股票', '10.02')
    const right = completedTabTask('right', '600003', '右侧股票', '10.03')
    const jobs = { left, middle, right }
    window.localStorage.setItem('ths_level2_stock_tabs_v2', JSON.stringify([
      { public_id: 'left', symbol: '600001', name: '左侧股票' },
      { public_id: 'middle', symbol: '600002', name: '中间股票' },
      { public_id: 'right', symbol: '600003', name: '右侧股票' },
    ]))
    window.localStorage.setItem('ths_level2_active_stock_tab', 'middle')
    vi.mocked(fetch).mockImplementation((input) => {
      const id = String(input).split('/').at(-1) as keyof typeof jobs
      return Promise.resolve(jsonResponse(jobs[id]))
    })
    const user = userEvent.setup()

    render(<App />)
    await screen.findByText('10.02')
    await user.click(screen.getByRole('button', { name: '删除 中间股票（600002）页签' }))
    await user.click(screen.getByRole('button', { name: '删除页签' }))

    expect(screen.queryByRole('tab', { name: /中间股票/ })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /右侧股票/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('10.03')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(JSON.parse(window.localStorage.getItem('ths_level2_stock_tabs_v2')!)).toEqual([
      { public_id: 'left', symbol: '600001', name: '左侧股票' },
      { public_id: 'right', symbol: '600003', name: '右侧股票' },
    ])
  })

  it('deletes a non-active tab without changing the active result', async () => {
    const first = completedTabTask('first', '600001', '第一股票', '10.01')
    const second = completedTabTask('second', '600002', '第二股票', '10.02')
    window.localStorage.setItem('ths_level2_stock_tabs_v2', JSON.stringify([
      { public_id: first.public_id, symbol: first.symbol, name: '第一股票' },
      { public_id: second.public_id, symbol: second.symbol, name: '第二股票' },
    ]))
    window.localStorage.setItem('ths_level2_active_stock_tab', 'first')
    vi.mocked(fetch).mockImplementation((input) => Promise.resolve(
      jsonResponse(String(input).endsWith('/second') ? second : first),
    ))
    const user = userEvent.setup()

    render(<App />)
    await screen.findByText('10.01')
    await user.click(screen.getByRole('button', { name: '删除 第二股票（600002）页签' }))
    await user.click(screen.getByRole('button', { name: '删除页签' }))

    expect(screen.getByRole('tab', { name: /第一股票/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('10.01')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('deletes the final local tab and supports Escape cancellation with focus return', async () => {
    const only = completedTabTask('only', '600001', '唯一股票', '10.01')
    window.localStorage.setItem('ths_level2_stock_tabs_v2', JSON.stringify([
      { public_id: only.public_id, symbol: only.symbol, name: '唯一股票' },
    ]))
    vi.mocked(fetch).mockResolvedValue(jsonResponse(only))
    const user = userEvent.setup()

    render(<App />)
    const deleteButton = await screen.findByRole('button', { name: '删除 唯一股票（600001）页签' })
    await user.click(deleteButton)
    expect(screen.getByRole('button', { name: '取消' })).toHaveFocus()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(deleteButton).toHaveFocus())

    await user.click(deleteButton)
    await user.click(screen.getByRole('button', { name: '删除页签' }))

    expect(screen.getByText('还没有采集记录')).toBeInTheDocument()
    expect(window.localStorage.getItem('ths_level2_stock_tabs_v2')).toBeNull()
    expect(window.localStorage.getItem('ths_level2_active_stock_tab')).toBeNull()
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
    expect(screen.queryByText('任务 public-job-1')).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /000001/ })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /600938/ })).toBeInTheDocument()
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
    await user.type(screen.getByLabelText('股票代码或名称'), '600938')
    await screen.findByText('中国海油（600938）')
    await user.click(screen.getByRole('button', { name: '提交采集任务' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    FakeEventSource.instances[0].emitStatus()
    await waitFor(() => expect(screen.getByText('正在采集')).toBeInTheDocument())
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0].closed).toBe(false)
  })

  it('does not scroll the document when an active job status refreshes', async () => {
    window.localStorage.clear()
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(jsonResponse(symbolLookup('600938', '中国海油')))
      .mockResolvedValueOnce(jsonResponse(task, 202))
      .mockResolvedValueOnce(jsonResponse({ ...task, status: 'RUNNING' })))
    const user = userEvent.setup()
    const scrollIntoView = vi.fn()
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    })

    try {
      render(<App />)
      await user.type(screen.getByLabelText('股票代码或名称'), '600938')
      await screen.findByText('中国海油（600938）')
      await user.click(screen.getByRole('button', { name: '提交采集任务' }))
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(1))

      FakeEventSource.instances[0].emitStatus()
      await waitFor(() => expect(screen.getByText('正在采集')).toBeInTheDocument())
      expect(scrollIntoView).toHaveBeenCalledTimes(1)
    } finally {
      Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
        configurable: true,
        value: originalScrollIntoView,
      })
    }
  })
})
