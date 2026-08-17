import { FormEvent, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createGraphEntity,
  createGraphRelation,
  getGraphNeighborhood,
  listGraphConflicts,
  mergeGraphEntities,
  resolveGraphConflict,
  reviewGraphFact,
  searchGraph,
  splitGraphEntity,
  updateGraphEntity,
  updateGraphRelation,
  type GraphConflict,
  type GraphEntity,
  type GraphFact,
} from "@/lib/api";
import { EmptyState, Loading, Modal, PageHeader, StatusBadge } from "@/components/ui";

const entityTypes = ["ORGANIZATION", "PERSON", "PRODUCT", "SYSTEM", "PROCESS", "POLICY", "STANDARD", "LOCATION", "PROJECT"];
const predicates = ["WORKS_FOR", "MANAGES", "OWNS", "PART_OF", "DEPENDS_ON", "USES", "PRODUCES", "APPLIES_TO", "COMPLIES_WITH", "LOCATED_IN", "RELATED_TO"];

export function GraphPage() {
  const queryClient = useQueryClient();
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<GraphEntity | null>(null);
  const [selectedFact, setSelectedFact] = useState<GraphFact | null>(null);
  const [entityDialog, setEntityDialog] = useState(false);
  const [relationDialog, setRelationDialog] = useState(false);
  const [mergeDialog, setMergeDialog] = useState(false);
  const [splitDialog, setSplitDialog] = useState(false);
  const [conflictDialog, setConflictDialog] = useState(false);
  const results = useQuery({
    queryKey: ["graph-search", query],
    queryFn: ({ signal }) => searchGraph(query, signal),
    enabled: Boolean(query),
  });
  const neighborhood = useQuery({
    queryKey: ["graph-neighborhood", selected?.id],
    queryFn: ({ signal }) => getGraphNeighborhood(selected?.id ?? "", signal),
    enabled: Boolean(selected),
  });
  const conflicts = useQuery({ queryKey: ["graph-conflicts"], queryFn: ({ signal }) => listGraphConflicts(signal) });
  const mutation = useMutation({
    mutationFn: (run: () => Promise<unknown>) => run(),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["graph-search"] }),
        queryClient.invalidateQueries({ queryKey: ["graph-neighborhood"] }),
        queryClient.invalidateQueries({ queryKey: ["graph-conflicts"] }),
      ]);
    },
  });
  function submitSearch(event: FormEvent) {
    event.preventDefault();
    if (input.trim()) setQuery(input.trim());
  }
  return (
    <div className="page-stack graph-page">
      <PageHeader eyebrow="Knowledge graph" title="知识图谱" description="搜索实体、展开局部关系，并在保留审计记录的前提下完成人工修正。" actions={<button className="primary-button" onClick={() => setEntityDialog(true)}>＋ 新建实体</button>} />
      {mutation.isError ? <div className="inline-error">{mutation.error.message}</div> : null}
      <section className="graph-toolbar panel">
        <form className="search-field graph-search" onSubmit={submitSearch}><span>⌕</span><input value={input} onChange={(event) => setInput(event.target.value)} placeholder="搜索人员、组织、系统、项目…" /><button type="submit">搜索</button></form>
        <div className="graph-stats"><span><strong>{results.data?.length ?? 0}</strong> 个结果</span><button onClick={() => setConflictDialog(true)}><strong>{conflicts.data?.length ?? 0}</strong> 个待处理冲突</button></div>
      </section>
      <div className="graph-workspace">
        <section className="panel graph-results">
          <div className="panel-heading"><div><span>Entities</span><h2>实体结果</h2></div></div>
          {!query ? <EmptyState title="搜索知识图谱" detail="输入实体名称后查看它的关系与来源证据。" /> : results.isPending ? <Loading /> : results.data?.length ? results.data.map((entity) => <button className={selected?.id === entity.id ? "entity-result active" : "entity-result"} key={entity.id} onClick={() => { setSelected(entity); setSelectedFact(null); }}><span className={`entity-type type-${entity.entity_type.toLowerCase()}`}>{entity.entity_type.slice(0, 2)}</span><div><strong>{entity.primary_name}</strong><small>{entity.entity_type} · {entity.origin}</small></div><StatusBadge value={entity.review_status} /></button>) : <EmptyState title="没有找到实体" detail="尝试名称的一部分，或创建新的人工实体。" />}
        </section>
        <section className="panel graph-canvas-panel">
          <div className="panel-heading"><div><span>Neighborhood</span><h2>局部关系图</h2></div>{selected ? <button onClick={() => setRelationDialog(true)}>＋ 添加关系</button> : null}</div>
          {!selected ? <EmptyState title="选择一个实体" detail="局部图只加载相关节点，不会把整张图一次性发送到浏览器。" /> : neighborhood.isPending ? <Loading label="正在展开关系" /> : neighborhood.data ? <LocalGraph center={selected} entities={neighborhood.data.entities} facts={neighborhood.data.facts} onSelectFact={setSelectedFact} /> : <EmptyState title="暂无关系" detail="该实体目前没有可用的在线关系。" />}
        </section>
        <aside className="panel graph-inspector">
          <div className="panel-heading"><div><span>Inspector</span><h2>{selectedFact ? "关系详情" : "实体详情"}</h2></div></div>
          {!selected ? <EmptyState title="暂无选择" detail="选择实体或关系后查看详细信息。" /> : selectedFact ? (
            <FactInspector fact={selectedFact} entities={[selected, ...(neighborhood.data?.entities ?? [])]} evidence={neighborhood.data?.evidence ?? []} onReview={(action) => reviewFact(selectedFact.id, action, mutation.mutate)} onEdit={() => editFact(selectedFact, mutation.mutate)} />
          ) : (
            <div className="inspector-content"><span className={`entity-type large type-${selected.entity_type.toLowerCase()}`}>{selected.entity_type.slice(0, 2)}</span><h3>{selected.primary_name}</h3><StatusBadge value={selected.review_status} /><dl><dt>实体类型</dt><dd>{selected.entity_type}</dd><dt>来源</dt><dd>{selected.origin}</dd><dt>Schema</dt><dd>{selected.schema_version}</dd><dt>别名</dt><dd>{(selected.aliases_json ?? selected.aliases ?? []).join("、") || "—"}</dd></dl><div className="review-actions"><button onClick={() => editEntity(selected, mutation.mutate)}>编辑实体</button><button onClick={() => setMergeDialog(true)}>合并重复实体</button>{neighborhood.data?.facts.length ? <button onClick={() => setSplitDialog(true)}>拆分实体</button> : null}</div></div>
          )}
        </aside>
      </div>
      {entityDialog ? <EntityDialog onClose={() => setEntityDialog(false)} onCreate={(payload) => mutation.mutate(() => createGraphEntity(payload), { onSuccess: () => setEntityDialog(false) })} pending={mutation.isPending} /> : null}
      {relationDialog && selected ? <RelationDialog subject={selected} options={(results.data ?? []).filter((entity) => entity.id !== selected.id)} onClose={() => setRelationDialog(false)} onCreate={(payload) => mutation.mutate(() => createGraphRelation(payload), { onSuccess: () => setRelationDialog(false) })} pending={mutation.isPending} /> : null}
      {mergeDialog && selected ? <MergeDialog target={selected} options={(results.data ?? []).filter((entity) => entity.id !== selected.id && entity.entity_type === selected.entity_type)} onClose={() => setMergeDialog(false)} onMerge={(sourceId, reason) => mutation.mutate(() => mergeGraphEntities({ target_entity_id: selected.id, source_entity_ids: [sourceId], reason }), { onSuccess: () => setMergeDialog(false) })} pending={mutation.isPending} /> : null}
      {splitDialog && selected && neighborhood.data ? <SplitDialog source={selected} facts={neighborhood.data.facts} onClose={() => setSplitDialog(false)} onSplit={(payload) => mutation.mutate(() => splitGraphEntity(selected.id, payload), { onSuccess: () => setSplitDialog(false) })} pending={mutation.isPending} /> : null}
      {conflictDialog ? <ConflictDialog conflicts={conflicts.data ?? []} onClose={() => setConflictDialog(false)} onResolve={(conflict, action) => resolveConflict(conflict, action, mutation.mutate)} /> : null}
    </div>
  );
}

function LocalGraph({ center, entities, facts, onSelectFact }: { center: GraphEntity; entities: GraphEntity[]; facts: GraphFact[]; onSelectFact: (fact: GraphFact) => void }) {
  if (!facts.length) return <EmptyState title="暂无在线关系" detail="可以通过受控表单新增一条符合 Schema 的关系。" />;
  return <div className="local-graph"><div className="center-node"><span>{center.entity_type.slice(0, 2)}</span><strong>{center.primary_name}</strong></div><div className="relation-rays">{facts.map((fact, index) => { const relatedId = fact.subject_entity_id === center.id ? fact.object_entity_id : fact.subject_entity_id; const entity = entities.find((value) => value.id === relatedId); return <button key={fact.id} className="graph-ray" style={{ "--ray-index": index } as CSSProperties} onClick={() => onSelectFact(fact)}><span className="ray-label">{fact.predicate}</span><span className="ray-line" /><span className="ray-node"><b>{entity?.entity_type.slice(0, 2) ?? "?"}</b><strong>{entity?.primary_name ?? "未知实体"}</strong></span></button>; })}</div></div>;
}

function FactInspector({ fact, entities, evidence, onReview, onEdit }: { fact: GraphFact; entities: GraphEntity[]; evidence: Array<{ fact_id: string; excerpt: string; source_locators: Array<Record<string, unknown>> }>; onReview: (action: "approve" | "reject") => void; onEdit: () => void }) {
  const subject = entities.find((entity) => entity.id === fact.subject_entity_id);
  const object = entities.find((entity) => entity.id === fact.object_entity_id);
  const sources = evidence.filter((item) => item.fact_id === fact.id);
  return <div className="inspector-content"><StatusBadge value={fact.review_status} /><div className="triple"><strong>{subject?.primary_name ?? "?"}</strong><span>{fact.predicate}</span><strong>{object?.primary_name ?? "?"}</strong></div><dl><dt>来源</dt><dd>{fact.origin}</dd><dt>置信度</dt><dd>{fact.confidence == null ? "—" : `${Math.round(fact.confidence * 100)}%`}</dd><dt>Schema</dt><dd>{fact.schema_version}</dd></dl><h4>来源证据</h4>{sources.length ? sources.map((source, index) => <blockquote key={index}>{source.excerpt}</blockquote>) : <p className="muted">人工关系或暂无可展示证据</p>}<div className="review-actions"><button onClick={onEdit}>修正关系</button>{fact.review_status === "unreviewed" ? <><button onClick={() => onReview("approve")}>确认事实</button><button className="danger-button" onClick={() => onReview("reject")}>驳回</button></> : null}</div></div>;
}

function EntityDialog({ onClose, onCreate, pending }: { onClose: () => void; onCreate: (value: { entity_type: string; primary_name: string; aliases: string[]; properties: Record<string, unknown>; reason: string }) => void; pending: boolean }) {
  const [type, setType] = useState(entityTypes[0]); const [name, setName] = useState(""); const [aliases, setAliases] = useState(""); const [reason, setReason] = useState("");
  return <Modal title="新建人工实体" onClose={onClose}><form className="stack-form" onSubmit={(event) => { event.preventDefault(); onCreate({ entity_type: type, primary_name: name, aliases: aliases.split(",").map((value) => value.trim()).filter(Boolean), properties: {}, reason }); }}><label className="field"><span>实体类型</span><select value={type} onChange={(event) => setType(event.target.value)}>{entityTypes.map((value) => <option key={value}>{value}</option>)}</select></label><label className="field"><span>标准名称</span><input required value={name} onChange={(event) => setName(event.target.value)} /></label><label className="field"><span>别名（逗号分隔）</span><input value={aliases} onChange={(event) => setAliases(event.target.value)} /></label><label className="field"><span>修正原因</span><textarea required minLength={3} value={reason} onChange={(event) => setReason(event.target.value)} /></label><div className="form-actions"><button type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={pending}>创建实体</button></div></form></Modal>;
}

function RelationDialog({ subject, options, onClose, onCreate, pending }: { subject: GraphEntity; options: GraphEntity[]; onClose: () => void; onCreate: (value: { subject_entity_id: string; predicate: string; object_entity_id: string; properties: Record<string, unknown>; reason: string }) => void; pending: boolean }) {
  const [predicate, setPredicate] = useState(predicates[0]); const [objectId, setObjectId] = useState(options[0]?.id ?? ""); const [reason, setReason] = useState("");
  return <Modal title="新增受控关系" onClose={onClose}><form className="stack-form" onSubmit={(event) => { event.preventDefault(); onCreate({ subject_entity_id: subject.id, predicate, object_entity_id: objectId, properties: {}, reason }); }}><div className="triple-preview"><strong>{subject.primary_name}</strong><span>→</span><strong>{options.find((item) => item.id === objectId)?.primary_name ?? "请选择目标"}</strong></div><label className="field"><span>关系类型</span><select value={predicate} onChange={(event) => setPredicate(event.target.value)}>{predicates.map((value) => <option key={value}>{value}</option>)}</select></label><label className="field"><span>目标实体</span><select required value={objectId} onChange={(event) => setObjectId(event.target.value)}><option value="" disabled>请选择</option>{options.map((value) => <option value={value.id} key={value.id}>{value.primary_name} · {value.entity_type}</option>)}</select></label><label className="field"><span>新增原因</span><textarea required minLength={3} value={reason} onChange={(event) => setReason(event.target.value)} /></label><div className="form-actions"><button type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={pending || !objectId}>创建关系</button></div></form></Modal>;
}

function MergeDialog({ target, options, onClose, onMerge, pending }: { target: GraphEntity; options: GraphEntity[]; onClose: () => void; onMerge: (sourceId: string, reason: string) => void; pending: boolean }) {
  const [sourceId, setSourceId] = useState(options[0]?.id ?? "");
  const [reason, setReason] = useState("");
  return <Modal title="合并重复实体" onClose={onClose}><form className="stack-form" onSubmit={(event) => { event.preventDefault(); onMerge(sourceId, reason); }}><div className="inline-warning">来源实体的关系和证据将转移到“{target.primary_name}”，来源实体随后删除。操作会记录审计。</div><label className="field"><span>待合并实体</span><select required value={sourceId} onChange={(event) => setSourceId(event.target.value)}><option value="" disabled>请选择同类型实体</option>{options.map((value) => <option value={value.id} key={value.id}>{value.primary_name}</option>)}</select></label><label className="field"><span>合并原因</span><textarea required minLength={3} value={reason} onChange={(event) => setReason(event.target.value)} /></label><div className="form-actions"><button type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={pending || !sourceId}>确认合并</button></div></form></Modal>;
}

function SplitDialog({ source, facts, onClose, onSplit, pending }: { source: GraphEntity; facts: GraphFact[]; onClose: () => void; onSplit: (value: { entity_type: string; primary_name: string; aliases: string[]; fact_ids: string[]; reason: string }) => void; pending: boolean }) {
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [factIds, setFactIds] = useState<string[]>(facts[0] ? [facts[0].id] : []);
  return <Modal title="拆分实体" onClose={onClose}><form className="stack-form" onSubmit={(event) => { event.preventDefault(); onSplit({ entity_type: source.entity_type, primary_name: name, aliases: [], fact_ids: factIds, reason }); }}><label className="field"><span>新实体名称</span><input required value={name} onChange={(event) => setName(event.target.value)} /></label><fieldset className="fact-picker"><legend>转移到新实体的关系</legend>{facts.map((fact) => <label key={fact.id}><input type="checkbox" checked={factIds.includes(fact.id)} onChange={(event) => setFactIds((current) => event.target.checked ? [...current, fact.id] : current.filter((id) => id !== fact.id))} /><span>{fact.predicate} · {fact.id.slice(0, 8)}</span></label>)}</fieldset><label className="field"><span>拆分原因</span><textarea required minLength={3} value={reason} onChange={(event) => setReason(event.target.value)} /></label><div className="form-actions"><button type="button" onClick={onClose}>取消</button><button className="primary-button" disabled={pending || !factIds.length}>确认拆分</button></div></form></Modal>;
}

function ConflictDialog({ conflicts, onClose, onResolve }: { conflicts: GraphConflict[]; onClose: () => void; onResolve: (conflict: GraphConflict, action: "resolve" | "dismiss") => void }) {
  return <Modal title="图谱冲突处理" onClose={onClose}>{conflicts.length ? <div className="conflict-list">{conflicts.map((conflict) => <article key={conflict.id}><header><div><strong>{conflict.conflict_type}</strong><span>{conflict.target_type} · {conflict.target_id.slice(0, 8)}</span></div><StatusBadge value={conflict.status} /></header><div className="conflict-compare"><pre>{JSON.stringify(conflict.current_json, null, 2)}</pre><span>→</span><pre>{JSON.stringify(conflict.proposed_json, null, 2)}</pre></div><footer><button onClick={() => onResolve(conflict, "dismiss")}>忽略建议</button><button className="primary-button" onClick={() => onResolve(conflict, "resolve")}>记录处置</button></footer></article>)}</div> : <EmptyState title="没有待处理冲突" detail="自动抽取与人工锁定事实冲突时会出现在这里。" />}</Modal>;
}

function editEntity(entity: GraphEntity, mutate: (run: () => Promise<unknown>) => void) {
  const name = window.prompt("实体标准名称", entity.primary_name); if (!name?.trim()) return;
  const aliases = window.prompt("别名（用逗号分隔）", (entity.aliases_json ?? []).join(",")); if (aliases === null) return;
  const reason = window.prompt("请输入修正原因"); if (!reason?.trim()) return;
  mutate(() => updateGraphEntity(entity.id, { primary_name: name.trim(), aliases: aliases.split(",").map((value) => value.trim()).filter(Boolean), reason }));
}

function editFact(fact: GraphFact, mutate: (run: () => Promise<unknown>) => void) {
  const predicate = window.prompt("关系类型", fact.predicate); if (!predicate?.trim()) return;
  const reason = window.prompt("请输入关系修正原因"); if (!reason?.trim()) return;
  mutate(() => updateGraphRelation(fact.id, { predicate: predicate.trim(), reason }));
}

function reviewFact(factId: string, action: "approve" | "reject", mutate: (run: () => Promise<unknown>) => void) {
  const reason = window.prompt(action === "approve" ? "请输入确认原因" : "请输入驳回原因");
  if (reason?.trim()) mutate(() => reviewGraphFact(factId, action, reason));
}

function resolveConflict(conflict: GraphConflict, action: "resolve" | "dismiss", mutate: (run: () => Promise<unknown>) => void) {
  const resolution = window.prompt(action === "resolve" ? "请输入冲突处置结论" : "请输入忽略原因");
  if (resolution?.trim()) mutate(() => resolveGraphConflict(conflict.id, action, resolution));
}
