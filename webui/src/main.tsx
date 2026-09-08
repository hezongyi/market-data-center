import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';

function App() {
  const [health, setHealth] = useState('loading');
  const [runs, setRuns] = useState<any[]>([]);
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [bars, setBars] = useState<any[]>([]);
  const [datasets, setDatasets] = useState<any[]>([]);
  useEffect(() => {
    Promise.all([fetch('/api/v1/health').then(r => r.json()), fetch('/api/v1/runs').then(r => r.json()), fetch('/api/v1/datasets').then(r => r.json())])
      .then(([h, r, d]) => { setHealth(h.data.status); setRuns(r.data); setDatasets(d.data); });
  }, []);
  const loadBars = () => fetch(`/api/v1/bars?symbol=${encodeURIComponent(symbol)}&timeframe=1d`).then(r => r.json()).then(r => setBars(r.data));
  return <main><header><h1>Market Data Center</h1><span className="status">{health}</span></header>
    <section><h2>Datasets</h2><ul>{datasets.map(d => <li key={d.dataset_id}>{d.dataset_id} ({d.schema_version})</li>)}</ul></section>
    <section><h2>Recent ingest runs</h2>{runs.length === 0 ? <p>No runs yet.</p> : <table><tbody>{runs.map(run => <tr key={run.run_id}><td>{run.run_id}</td><td>{run.dataset_id}</td><td>{run.status}</td><td>{run.row_count}</td></tr>)}</tbody></table>}</section>
    <section><h2>Data Explorer</h2><input value={symbol} onChange={e => setSymbol(e.target.value)} /><button onClick={loadBars}>Load bars</button>{bars.length > 0 && <table><tbody>{bars.map((bar, i) => <tr key={i}><td>{String(bar.bar_ts)}</td><td>{bar.open}</td><td>{bar.high}</td><td>{bar.low}</td><td>{bar.close}</td></tr>)}</tbody></table>}</section>
  </main>;
}
createRoot(document.getElementById('root')!).render(<App />);
