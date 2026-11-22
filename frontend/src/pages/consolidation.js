import {feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function versionPill(item) {
  const label = item.is_active ? '当前生效' : ({superseded: '已替换', active: '生效中'}[item.status] || item.status);
  const state = item.is_active ? 'active' : item.status;
  return pill(label, state);
}

function contradictionPanel(contradictions) {
  if (!contradictions?.length) return null;
  return panel('归并矛盾（已保留当前生效版本）',
    p('以下人工判定与半小时窗口或灯组规则冲突，未生成新版本；请调整判定后重新归并。', 'feedback'),
    table([
      {key: 'type', label: '矛盾类型'},
      {key: 'message', label: '说明'},
      {label: '涉及报障', render: row => (row.report_refs || []).join('、')},
    ], contradictions, {caption: '矛盾记录', empty: '无'}));
}

function judgmentForm(datasetId, error, refresh) {
  const submit = button('保存人工判定', {type: 'submit', class: 'primary'});
  return el('form', {class: 'fields', onSubmit: handler(submit, error, async event => {
    const data = new FormData(event.target);
    await workspace.saveJudgment(datasetId, {
      left_ref: data.get('left_ref'), right_ref: data.get('right_ref'),
      relation: data.get('relation'), note: data.get('note') || '',
    });
    event.target.reset();
    await refresh();
  })},
    field({key: 'left_ref', label: '报障编号 A', type: 'string', max_length: 64}),
    field({key: 'right_ref', label: '报障编号 B', type: 'string', max_length: 64}),
    el('label', {class: 'field', for: 'relation'}, el('span', {}, '认定关系'),
      el('select', {id: 'relation', name: 'relation'},
        el('option', {value: 'same'}, '同一故障（必须同单）'),
        el('option', {value: 'separate'}, '不同故障（必须分单）'))),
    field({key: 'note', label: '判定依据（可选）', type: 'string', required: false, max_length: 500}),
    submit, error);
}

export async function consolidationPage(route, refresh) {
  const {spec, datasets} = workspace;
  const selected = datasets.find(item => item.id === route.id) || datasets[0];
  if (!selected) return panel(spec.pages[2], empty('请先在灯组与工单页导入含原始报障的批次。', '#inputs', '前往导入'));
  const datasetId = selected.id;
  const error = feedback();
  const state = {contradictions: null};
  const [versions, judgments, detail] = await Promise.all([
    workspace.versions(datasetId), workspace.judgments(datasetId), workspace.getDataset(datasetId),
  ]);
  const active = versions.find(item => item.is_active) || null;
  const activeDetail = active ? await workspace.activeVersion(datasetId) : null;

  const runButton = session.canWrite && button('重新归并并发布', {
    class: 'primary', 'data-testid': 'run-consolidation',
    onClick: handler(button, error, async () => {
      try {
        await workspace.runConsolidation(datasetId);
        state.contradictions = null;
      } catch (err) {
        // 422 矛盾：后端返回结构化 contradictions，旧版保留，不做页面级抛错。
        state.contradictions = err.contradictions || null;
        error.textContent = err.message || '归并存在矛盾，已保留旧版。';
        return;
      }
      await refresh();
    }),
  });

  const rawCount = detail.rows.filter(row => row.record_type === '原始报障').length;
  const summary = panel('归并口径与当前版本',
    p(`批次「${selected.name}」含 ${rawCount} 条原始报障。同一灯组、最早最晚相差不超过 30 分钟的报障归为同一工单；先让工单数最少，同数时尽量保留上一版同单的报障对。`, 'subtle'),
    active ? el('div', {class: 'decision'},
      versionPill(active),
      p(`版本 V${active.version_no} · ${active.statistics?.order_count ?? '-'} 张工单 · ${active.statistics?.report_count ?? '-'} 条报障 · 保留旧版同单对 ${active.statistics?.preserved_pairs ?? '-'}/${active.statistics?.legacy_pair_total ?? '-'}`, 'filename'),
      p(`生成于 ${date(active.created_at)} · ${active.author}`, 'subtle'))
      : p('还没有生效版本。补录报障或登记人工判定后点击下方按钮发布首个版本。', 'empty'),
    runButton, error);

  const orderColumns = [
    {key: 'order_no', label: '工单号'},
    {key: 'site_id', label: '灯组编号'},
    {key: 'first_at', label: '最早报障', render: row => row.first_at?.replace('T', ' ').slice(0, 16)},
    {key: 'last_at', label: '最晚报障', render: row => row.last_at?.replace('T', ' ').slice(0, 16)},
    {key: 'span_minutes', label: '跨度(分钟)'},
    {key: 'report_count', label: '原始报障数'},
    {key: 'sources', label: '来源', render: row => row.sources?.join('、')},
    {key: 'report_refs', label: '原始报障编号', render: row => row.report_refs?.join('、')},
  ];
  const orderTable = activeDetail ? panel('工单与原始报障溯源',
    p('每张工单可查到其包含的全部热线、巡检原始报障。', 'subtle'),
    table(orderColumns, activeDetail.payload.orders,
      {caption: '工单与原始报障溯源', pageSize: 12}))
    : panel('工单与原始报障溯源', empty('发布归并版本后在此查看工单与报障对应关系。'));

  const versionTable = panel('历史版本（一次性切换、旧版保留）', table([
    {key: 'version_no', label: '版本', render: row => `V${row.version_no}`},
    {label: '状态', render: versionPill},
    {label: '工单数', render: row => row.statistics?.order_count ?? '-'},
    {label: '保留报障对', render: row => `${row.statistics?.preserved_pairs ?? '-'} / ${row.statistics?.legacy_pair_total ?? '-'}`},
    {key: 'author', label: '操作人'},
    {label: '生成时间', render: row => date(row.created_at)},
  ], versions, {caption: '归并版本', empty: '暂无版本。'}));

  const judgmentTable = panel('人工判定记录', table([
    {key: 'left_ref', label: '报障 A'},
    {key: 'right_ref', label: '报障 B'},
    {label: '关系', render: row => row.relation === 'same' ? '同一故障' : '不同故障'},
    {key: 'note', label: '依据'},
    {key: 'author', label: '登记人'},
    {label: '操作', render: row => session.canReview ? button('删除', {
      class: 'quiet',
      onClick: handler(button, error, async () => { await workspace.removeJudgment(datasetId, row.id); await refresh(); }),
    }) : '—'},
  ], judgments, {caption: '人工判定', empty: '尚无人工判定。'}));

  const judgmentPanel = session.canReview
    ? panel('登记人工判定', p('同一故障会强制同单，不同故障会强制分单；判定与半小时窗口矛盾时归并将列出矛盾并保留旧版。', 'subtle'),
      judgmentForm(datasetId, feedback(), refresh), judgmentTable)
    : panel('人工判定', p('仅复核主管可登记人工判定。', 'subtle'), judgmentTable);

  return el('div', {class: 'two-col'},
    el('div', {class: 'wide-panel'},
      el('div', {class: 'actions'},
        el('label', {class: 'sr', for: 'cons-dataset'}, '选择数据批次'),
        el('select', {id: 'cons-dataset', 'ariaLabel': '选择数据批次',
          onChange: event => { location.hash = `#consolidation/${event.target.value}`; }},
          datasets.map(item => el('option', {value: item.id, selected: item.id === datasetId}, item.name))),
        link('用当前版本发起测算', `#run/${datasetId}`, {class: 'quiet'})),
      summary, contradictionPanel(state.contradictions), orderTable, versionTable),
    el('aside', {class: 'sidebar-panel'}, judgmentPanel));
}
