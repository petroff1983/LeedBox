(() => {
  const form = document.getElementById('legal-form');
  const source = document.getElementById('legal-source');
  if (!form || !source || !window.Quill) return;
  const visual = document.getElementById('visual-panel');
  const code = document.getElementById('html-panel');
  const note = document.getElementById('editor-note');
  document.getElementById('legal-modes').hidden = false;
  // Quill's core does not round-trip HTML tables: keep source mode, never discard them.
  if (source.dataset.sanitized !== '1' || /<(?:table|thead|tbody|td|th|pre|code|hr)\b/i.test(source.value)) {
    note.textContent = 'Используйте HTML и предпросмотр после серверной очистки. Таблицы и блоки кода сохраняются без преобразования.';
    document.getElementById('visual-mode').disabled = true;
    return;
  }
  const editor = new Quill('#legal-editor', {
    theme: null, modules: {toolbar: '#legal-toolbar'},
    formats: ['header', 'bold', 'italic', 'underline', 'strike', 'list', 'blockquote', 'link', 'indent', 'align', 'script'],
  });
  editor.setContents(editor.clipboard.convert({html: source.value}));
  editor.root.setAttribute('aria-label', 'Текст документа');
  editor.root.setAttribute('role', 'textbox');
  editor.root.setAttribute('aria-multiline', 'true');
  let visualMode = true;
  let visualDirty = false;
  editor.on('text-change', (_delta, _previous, origin) => {
    if (origin === 'user') visualDirty = true;
  });
  let sourceChanged = false;
  const switchMode = (isVisual) => {
    visual.hidden = !isVisual; code.hidden = isVisual;
    document.getElementById('visual-mode').setAttribute('aria-pressed', String(isVisual));
    document.getElementById('html-mode').setAttribute('aria-pressed', String(!isVisual));
    note.textContent = isVisual ? 'Выделите текст и выберите оформление. Для проверки результата используйте предпросмотр.' : 'HTML очищается сервером. Возврат в визуальный режим после правок выполнит безопасный предпросмотр.';
    visualMode = isVisual;
  };
  switchMode(true);
  document.getElementById('html-mode').addEventListener('click', () => {
    if (visualMode && visualDirty) source.value = editor.getSemanticHTML();
    switchMode(false);
  });
  source.addEventListener('input', () => {sourceChanged = true;});
  document.getElementById('visual-mode').addEventListener('click', () => {
    if (!visualMode && sourceChanged) {
      form.requestSubmit(document.getElementById('legal-preview-button'));
    } else switchMode(true);
  });
  let selection = null;
  editor.on('selection-change', range => {if (range) selection = range;});
  document.getElementById('add-link').addEventListener('click', () => {
    const url = document.getElementById('legal-link-url').value.trim();
    const status = document.getElementById('link-status');
    if (!selection || !selection.length || !/^https?:\/\//i.test(url)) {
      status.textContent = 'Выделите текст и введите полный HTTP(S)-адрес.'; return;
    }
    editor.formatText(selection.index, selection.length, 'link', url, 'user');
    status.textContent = 'Ссылка добавлена.';
  });
  form.addEventListener('submit', () => {
    // Opening a document and changing its title must not rewrite its original HTML.
    if (visualMode && visualDirty) source.value = editor.getSemanticHTML();
  });
})();
