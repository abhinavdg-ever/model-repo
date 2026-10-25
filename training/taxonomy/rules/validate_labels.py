import json,csv,collections
t=json.load(open('taxonomy_v29.json'))
L2={'Progress Note':t['progress_note']['L2_values'],'Lab Report':t['lab_report_L2'],'Operative Note':t['operative_note_L2'],'Procedure Note':t['procedure_note_L2'],'Discharge Summary':t['discharge_note_L2'],'Clinical Summary':t['clinical_summary_L2'],'Consent and Authorization':t['consent_auth_L2'],'Demographics':t['demographics_L2'],'Immunization':t['immunization_L2'],'Medication Record':t['medication_record_L2'],'Order and Requisition':t['order_requisition_L2'],'Problem List':t['problem_list_L2'],'Forms and Questionnaires':t['forms_L2'],'Provider Letter':t['provider_letter_L2'],'Patient Communication':t['letters_messages_L2'],'Administrative':t['administrative_L2'],'Others':[]}
L3=t['lab_report_L3']
rows=list(csv.DictReader(open('image_labels.csv'))); recs=[json.loads(l) for l in open('training_data.jsonl')]
R={r['image']:r for r in rows}
bad=collections.Counter()
for r in rows:
    if r['L1'] not in L2: bad['L1 '+r['L1']]+=1
    elif r['L2'] and r['L2'] not in L2[r['L1']]: bad[f"L2 {r['L1']}>{r['L2']}"]+=1
    elif not r['L2'] and L2[r['L1']]: bad[f"blank L2 under {r['L1']}"]+=1
    if r['L3'] and (r['L1']!='Lab Report' or r['L3'] not in L3.get(r['L2'],[])): bad[f"L3 {r['L2']}>{r['L3']}"]+=1
    if r['specialty'] not in t['specialty_values']: bad['spec '+r['specialty']]+=1
    if r['path']!=' > '.join(x for x in (r['L1'],r['L2'],r['L3']) if x): bad['path']+=1
print('label problems:',dict(bad))
mis=collections.Counter()
for j in recs:
    r=R[j['image']]
    for k in ('L1','L2','L3','path','specialty','chart_id','codeability'):
        if str(j[k] or '')!=str(r[k] or ''): mis[k]+=1
    if j['L2_train']!=(f"{r['L1']} > {r['L2']}" if r['L2'] else r['L1']): mis['L2_train']+=1
print('jsonl vs csv mismatches:',dict(mis),'| records',len(recs),'rows',len(rows),'| jsonl not in csv',len({j['image'] for j in recs}-set(R)))
vio=[]
for src in ('page_type_overrides','family_map'):
  for k,v in t[src].items():
    if isinstance(v,dict) and v.get('L1') and (v['L1'] not in L2 or (v.get('L2') and v['L2'] not in L2[v['L1']])): vio.append((src,k))
for l1,m in t['L2_merge'].items():
    if l1.startswith('_'): continue
    for s,d in m.items():
        if s.startswith('_'): continue
        if d.startswith('__move:') and d[7:] not in L2: vio.append(('move',l1,s,d))
        elif not d.startswith('__') and d not in L2.get(l1,[]): vio.append(('merge',l1,s,d))
print('taxonomy validator violations:',vio)
print('procedure L2 dup check', [x for x in t['procedure_note_L2'] if 'EGD' in x])
e=json.load(open('encounter_canon.json')); print('encounter prior keys not an L1:',[k for k in e['document_type_priors'] if not k.startswith('_') and k not in L2])
print('unreviewed now', collections.Counter(r['label_agrees_with_text'] for r in rows))
