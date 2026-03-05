import { useEffect, useRef, useState } from 'react'
import Editor from '@monaco-editor/react'
import axios from 'axios'
import toast from 'react-hot-toast'
import { Upload, Code2, List, Save, Trash2, RefreshCw, CheckCircle, XCircle, FileCode } from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────
type Tab = 'registry' | 'editor' | 'upload'

interface RegistryEntry {
  key:         string
  class_name:  string
  description: string
  asset_class: string
  broker:      string
  file:        string | null
  builtin:     boolean
  db_usages:   { id: number; name: string; active: boolean }[]
}

// ── Empty placeholder code for new files ─────────────────────────────────────
const TEMPLATE = `"""
My Custom Strategy
==================
"""

import pandas as pd
from typing import Optional
from loguru import logger

from core.strategies.base import BaseStrategy, Signal


class MyCustomStrategy(BaseStrategy):
    name        = "my_custom_strategy"
    description = "Describe your strategy here."
    asset_class = "crypto"
    broker      = "binance"

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str = "1h",
        tool_outputs: Optional[dict] = None,
        **kwargs,
    ) -> Signal:
        current_price = float(data["close"].iloc[-1])
        # TODO: implement your logic here
        return Signal(
            symbol=symbol, signal="HOLD",
            entry_price=current_price,
            stop_loss=None, take_profit=None,
            confidence=0.0,
            timeframe=timeframe,
            strategy_name=self.name,
            asset_class=self.asset_class,
            broker=self.broker,
            reasons=["Not implemented"],
        )
`

// ── Main page ─────────────────────────────────────────────────────────────────
export default function StrategyEditor() {
  const [tab, setTab]               = useState<Tab>('registry')
  const [registry, setRegistry]     = useState<RegistryEntry[]>([])
  const [regLoading, setRegLoading] = useState(true)

  // Editor tab state
  const [editKey, setEditKey]       = useState<string | null>(null)
  const [editorCode, setEditorCode] = useState<string>(TEMPLATE)
  const [saving, setSaving]         = useState(false)
  const [loadingCode, setLoadingCode] = useState(false)

  // Upload tab state
  const [dragOver, setDragOver]     = useState(false)
  const [uploading, setUploading]   = useState(false)
  const [uploadResult, setUploadResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const fileInputRef                = useRef<HTMLInputElement>(null)

  const fetchRegistry = async () => {
    setRegLoading(true)
    try {
      const r = await axios.get('/api/strategy-code/registry')
      setRegistry(r.data.registry)
    } catch { toast.error('Failed to load registry') }
    setRegLoading(false)
  }

  useEffect(() => { fetchRegistry() }, [])

  // ── Load strategy into editor ─────────────────────────────────────────────
  const openInEditor = async (key: string) => {
    setLoadingCode(true)
    setEditKey(key)
    setTab('editor')
    try {
      const r = await axios.get(`/api/strategy-code/${key}`, { responseType: 'text' })
      setEditorCode(typeof r.data === 'string' ? r.data : JSON.stringify(r.data, null, 2))
    } catch { toast.error(`Could not load source for '${key}'`) }
    setLoadingCode(false)
  }

  const openNewTemplate = () => {
    setEditKey(null)
    setEditorCode(TEMPLATE)
    setTab('editor')
  }

  // ── Save (PUT) ────────────────────────────────────────────────────────────
  const handleSave = async () => {
    if (!editKey) {
      toast.error('Select a strategy from the Registry tab first, or upload a new file.')
      return
    }
    setSaving(true)
    try {
      const r = await axios.put(`/api/strategy-code/${editKey}`, { code: editorCode })
      toast.success(r.data.message || 'Saved and hot-reloaded.')
      fetchRegistry()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Save failed')
    }
    setSaving(false)
  }

  // ── Delete ────────────────────────────────────────────────────────────────
  const handleDelete = async (key: string) => {
    if (!confirm(`Delete strategy '${key}'? This cannot be undone.`)) return
    try {
      await axios.delete(`/api/strategy-code/${key}`)
      toast.success(`'${key}' deleted.`)
      if (editKey === key) { setEditKey(null); setEditorCode(TEMPLATE) }
      fetchRegistry()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Delete failed')
    }
  }

  // ── Upload ────────────────────────────────────────────────────────────────
  const doUpload = async (file: File) => {
    if (!file.name.endsWith('.py')) {
      setUploadResult({ ok: false, msg: 'Only .py files accepted.' })
      return
    }
    setUploading(true)
    setUploadResult(null)
    const fd = new FormData()
    fd.append('file', file)
    try {
      const r = await axios.post('/api/strategy-code/upload', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      setUploadResult({ ok: true, msg: r.data.message })
      toast.success(r.data.message)
      fetchRegistry()
    } catch (e: any) {
      const msg = e?.response?.data?.detail || 'Upload failed'
      setUploadResult({ ok: false, msg })
      toast.error(msg)
    }
    setUploading(false)
  }

  const onFilePick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (f) doUpload(f)
    e.target.value = ''
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files[0]
    if (f) doUpload(f)
  }

  // ── Tab buttons ─────────────────────────────────────────────────────────────
  const TabBtn = ({ id, icon: Icon, label }: { id: Tab; icon: any; label: string }) => (
    <button
      onClick={() => setTab(id)}
      className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 transition-all ${
        tab === id
          ? 'border-brand-500 text-brand-400'
          : 'border-transparent text-gray-500 hover:text-gray-300'
      }`}
    >
      <Icon size={14} />
      {label}
    </button>
  )

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-6 pt-6 pb-0">
        <div className="flex items-center justify-between mb-1">
          <div>
            <h1 className="text-lg font-bold text-white">Strategy Editor</h1>
            <p className="text-xs text-gray-500 mt-0.5">Upload, edit, and hot-reload strategy files — no restart needed</p>
          </div>
          <button onClick={openNewTemplate} className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition-colors">
            <FileCode size={13} /> New from template
          </button>
        </div>
        {/* Tabs */}
        <div className="flex border-b border-dark-600 mt-4">
          <TabBtn id="registry" icon={List}    label="Registry" />
          <TabBtn id="editor"   icon={Code2}   label={editKey ? `Edit: ${editKey}` : 'Editor'} />
          <TabBtn id="upload"   icon={Upload}  label="Upload" />
        </div>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-auto p-6">

        {/* ── REGISTRY TAB ─────────────────────────────────────────────────── */}
        {tab === 'registry' && (
          <div>
            <div className="flex items-center justify-between mb-4">
              <p className="text-xs text-gray-500">{registry.length} strategies registered</p>
              <button onClick={fetchRegistry} className="flex items-center gap-1 text-xs text-gray-500 hover:text-white transition-colors">
                <RefreshCw size={11} className={regLoading ? 'animate-spin' : ''} /> Refresh
              </button>
            </div>
            {regLoading ? (
              <p className="text-xs text-gray-600">Loading…</p>
            ) : (
              <div className="space-y-3">
                {registry.map(entry => (
                  <div key={entry.key} className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap mb-1">
                        <span className="text-sm font-semibold text-white font-mono">{entry.key}</span>
                        {entry.builtin && (
                          <span className="text-[10px] bg-brand-500/15 text-brand-400 px-1.5 py-0.5 rounded">built-in</span>
                        )}
                        {entry.asset_class && (
                          <span className="text-[10px] bg-dark-700 text-gray-500 px-1.5 py-0.5 rounded">{entry.asset_class}</span>
                        )}
                        {entry.broker && (
                          <span className="text-[10px] bg-dark-700 text-gray-500 px-1.5 py-0.5 rounded">{entry.broker}</span>
                        )}
                      </div>
                      <p className="text-xs text-gray-500 mb-1.5 line-clamp-2">{entry.description || 'No description'}</p>
                      <div className="flex items-center gap-3 text-xs text-gray-600">
                        <span>Class: <span className="text-gray-400 font-mono">{entry.class_name}</span></span>
                        {entry.file && <span>File: <span className="text-gray-400 font-mono">{entry.file}</span></span>}
                        {entry.db_usages.length > 0 && (
                          <span className="text-yellow-600">{entry.db_usages.length} DB strategy row{entry.db_usages.length > 1 ? 's' : ''}</span>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <button
                        onClick={() => openInEditor(entry.key)}
                        className="flex items-center gap-1 text-xs text-brand-400 hover:text-brand-300 transition-colors px-2 py-1 rounded bg-brand-500/10 hover:bg-brand-500/20"
                      >
                        <Code2 size={11} /> Edit
                      </button>
                      {!entry.builtin && (
                        <button
                          onClick={() => handleDelete(entry.key)}
                          className="flex items-center gap-1 text-xs text-red-400 hover:text-red-300 transition-colors px-2 py-1 rounded bg-red-900/10 hover:bg-red-900/20"
                        >
                          <Trash2 size={11} /> Delete
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* ── EDITOR TAB ───────────────────────────────────────────────────── */}
        {tab === 'editor' && (
          <div className="space-y-3 h-full">
            {!editKey && (
              <div className="flex items-center gap-2 p-3 bg-yellow-900/20 border border-yellow-800/40 rounded-lg text-xs text-yellow-400">
                <span>No strategy selected. Open one from the Registry tab, or start editing below to upload as a new file.</span>
              </div>
            )}
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500 font-mono">
                {editKey ? `Editing: ${editKey}` : 'New strategy (read-only preview — upload to save)'}
              </span>
              {editKey && (
                <button
                  onClick={handleSave}
                  disabled={saving || loadingCode}
                  className="flex items-center gap-1.5 text-xs bg-brand-500 hover:bg-brand-600 text-white px-3 py-1.5 rounded-lg transition-all disabled:opacity-50 font-medium"
                >
                  <Save size={12} />
                  {saving ? 'Saving…' : 'Save & Hot-Reload'}
                </button>
              )}
            </div>
            <div className="rounded-xl overflow-hidden border border-dark-600" style={{ height: 'calc(100vh - 280px)', minHeight: 400 }}>
              {loadingCode ? (
                <div className="flex items-center justify-center h-full bg-dark-900 text-gray-500 text-sm">Loading source…</div>
              ) : (
                <Editor
                  height="100%"
                  defaultLanguage="python"
                  theme="vs-dark"
                  value={editorCode}
                  onChange={v => setEditorCode(v ?? '')}
                  options={{
                    fontSize: 13,
                    minimap: { enabled: false },
                    scrollBeyondLastLine: false,
                    wordWrap: 'on',
                    lineNumbers: 'on',
                    renderLineHighlight: 'line',
                    bracketPairColorization: { enabled: true },
                    readOnly: !editKey,
                  }}
                />
              )}
            </div>
          </div>
        )}

        {/* ── UPLOAD TAB ───────────────────────────────────────────────────── */}
        {tab === 'upload' && (
          <div className="max-w-lg mx-auto space-y-4">
            <p className="text-xs text-gray-500">
              Drop a <span className="font-mono text-gray-300">.py</span> file containing a single class that subclasses{' '}
              <span className="font-mono text-gray-300">BaseStrategy</span>. It will be validated, saved to{' '}
              <span className="font-mono text-gray-300">core/strategies/</span>, and immediately registered — no restart needed.
            </p>

            {/* Drop zone */}
            <div
              onDragOver={e => { e.preventDefault(); setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
              onClick={() => fileInputRef.current?.click()}
              className={`cursor-pointer border-2 border-dashed rounded-xl p-12 flex flex-col items-center gap-3 transition-all ${
                dragOver
                  ? 'border-brand-500 bg-brand-500/10'
                  : 'border-dark-600 hover:border-dark-500 bg-dark-800'
              }`}
            >
              <Upload size={32} className={dragOver ? 'text-brand-400' : 'text-gray-600'} />
              <div className="text-center">
                <p className="text-sm text-gray-300 font-medium">Drop your .py file here</p>
                <p className="text-xs text-gray-600 mt-1">or click to browse</p>
              </div>
              <input ref={fileInputRef} type="file" accept=".py" onChange={onFilePick} className="hidden" />
            </div>

            {/* Upload status */}
            {uploading && (
              <div className="flex items-center gap-2 text-xs text-gray-400 bg-dark-800 border border-dark-600 rounded-lg p-3">
                <RefreshCw size={13} className="animate-spin" /> Validating and registering…
              </div>
            )}
            {uploadResult && !uploading && (
              <div className={`flex items-start gap-2 text-xs rounded-lg p-3 border ${
                uploadResult.ok
                  ? 'bg-green-900/20 border-green-800/40 text-green-400'
                  : 'bg-red-900/20 border-red-800/40 text-red-400'
              }`}>
                {uploadResult.ok ? <CheckCircle size={14} className="shrink-0 mt-0.5" /> : <XCircle size={14} className="shrink-0 mt-0.5" />}
                <span className="leading-relaxed">{uploadResult.msg}</span>
              </div>
            )}

            <p className="text-xs text-gray-700">
              Requirements: class must subclass <span className="font-mono">BaseStrategy</span>, implement{' '}
              <span className="font-mono">generate_signal()</span>, and have a unique <span className="font-mono">name</span> class attribute.
            </p>
          </div>
        )}

      </div>
    </div>
  )
}
