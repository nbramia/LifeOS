// Chat composer attachments (#358): drag/drop, paste, file-picker, validation,
// preview, and the base64 payload sent with a message. Extracted verbatim from
// index.html's inline <script>.

import { state, elements } from './session.js';
import { escapeHtml } from './thread.js';

// Attachment configuration
const ALLOWED_TYPES = {
  'image/png': 5 * 1024 * 1024,
  'image/jpeg': 5 * 1024 * 1024,
  'image/jpg': 5 * 1024 * 1024,
  'image/gif': 5 * 1024 * 1024,
  'image/webp': 5 * 1024 * 1024,
  'application/pdf': 10 * 1024 * 1024,
  'text/plain': 1 * 1024 * 1024,
  'text/markdown': 1 * 1024 * 1024,
  'text/csv': 1 * 1024 * 1024,
  'application/json': 1 * 1024 * 1024,
};
const MAX_ATTACHMENTS = 5;
const MAX_TOTAL_SIZE = 20 * 1024 * 1024;

export function setupAttachmentHandlers() {
  const { inputArea, fileInput } = elements;
  // Drag and drop
  inputArea.addEventListener('dragenter', handleDragEnter);
  inputArea.addEventListener('dragover', handleDragOver);
  inputArea.addEventListener('dragleave', handleDragLeave);
  inputArea.addEventListener('drop', handleDrop);

  // File input change
  fileInput.addEventListener('change', handleFileSelect);

  // Paste handler
  document.addEventListener('paste', handlePaste);
}

function handleDragEnter(e) {
  e.preventDefault();
  e.stopPropagation();
  elements.inputArea.classList.add('drag-over');
}

function handleDragOver(e) {
  e.preventDefault();
  e.stopPropagation();
  elements.inputArea.classList.add('drag-over');
}

function handleDragLeave(e) {
  e.preventDefault();
  e.stopPropagation();
  // Only remove if leaving the input area entirely
  if (!elements.inputArea.contains(e.relatedTarget)) {
    elements.inputArea.classList.remove('drag-over');
  }
}

function handleDrop(e) {
  e.preventDefault();
  e.stopPropagation();
  elements.inputArea.classList.remove('drag-over');

  const files = e.dataTransfer.files;
  if (files.length > 0) {
    addFiles(Array.from(files));
  }
}

function handleFileSelect(e) {
  const files = e.target.files;
  if (files.length > 0) {
    addFiles(Array.from(files));
  }
  // Reset input so same file can be selected again
  elements.fileInput.value = '';
}

function handlePaste(e) {
  // Only handle paste if input field or input area is focused
  if (document.activeElement !== elements.inputField && !elements.inputArea.contains(document.activeElement)) {
    return;
  }

  const items = e.clipboardData?.items;
  if (!items) return;

  const files = [];
  for (const item of items) {
    if (item.kind === 'file') {
      const file = item.getAsFile();
      if (file) {
        files.push(file);
      }
    }
  }

  if (files.length > 0) {
    e.preventDefault(); // Prevent default paste behavior for files
    addFiles(files);
  }
  // If no files, allow normal text paste
}

export function openFilePicker() {
  elements.fileInput.click();
}

async function addFiles(files) {
  for (const file of files) {
    await addFile(file);
  }
}

async function addFile(file) {
  // Validate file type
  if (!ALLOWED_TYPES[file.type]) {
    showAttachmentError(`Unsupported file type: ${file.type || 'unknown'}`);
    return;
  }

  // Validate file size
  const maxSize = ALLOWED_TYPES[file.type];
  if (file.size > maxSize) {
    const maxMB = (maxSize / (1024 * 1024)).toFixed(0);
    showAttachmentError(`File too large. Max ${maxMB}MB for ${file.type}`);
    return;
  }

  // Check attachment count
  if (state.attachments.length >= MAX_ATTACHMENTS) {
    showAttachmentError(`Maximum ${MAX_ATTACHMENTS} attachments allowed`);
    return;
  }

  // Check total size
  const currentTotal = state.attachments.reduce((sum, a) => sum + a.sizeBytes, 0);
  if (currentTotal + file.size > MAX_TOTAL_SIZE) {
    showAttachmentError('Total attachment size exceeds 20MB limit');
    return;
  }

  // Read file as data URL
  try {
    const dataUrl = await readFileAsDataUrl(file);
    const attachment = {
      id: Date.now() + Math.random(),
      file: file,
      dataUrl: dataUrl,
      filename: file.name,
      mediaType: file.type,
      sizeBytes: file.size
    };
    state.attachments.push(attachment);
    renderAttachments();
  } catch (err) {
    console.error('Error reading file:', err);
    showAttachmentError('Failed to read file');
  }
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export function removeAttachment(id) {
  state.attachments = state.attachments.filter(a => a.id !== id);
  renderAttachments();
}

function renderAttachments() {
  const { attachmentsPreview } = elements;
  if (state.attachments.length === 0) {
    attachmentsPreview.classList.remove('visible');
    attachmentsPreview.innerHTML = '';
    return;
  }

  attachmentsPreview.classList.add('visible');
  attachmentsPreview.innerHTML = state.attachments.map(att => {
    const isImage = att.mediaType.startsWith('image/');
    const icon = getFileIcon(att.mediaType);
    const sizeStr = formatFileSize(att.sizeBytes);

    return `
                    <div class="attachment-item">
                        <div class="attachment-thumb">
                            ${isImage ? `<img src="${att.dataUrl}" alt="${escapeHtml(att.filename)}">` : icon}
                        </div>
                        <div class="attachment-info">
                            <div class="attachment-name" title="${escapeHtml(att.filename)}">${escapeHtml(att.filename)}</div>
                            <div class="attachment-size">${sizeStr}</div>
                        </div>
                        <button class="attachment-remove" onclick="removeAttachment(${att.id})" title="Remove">×</button>
                    </div>
                `;
  }).join('');
}

function getFileIcon(mediaType) {
  if (mediaType === 'application/pdf') return '📄';
  if (mediaType.startsWith('text/')) return '📝';
  if (mediaType === 'application/json') return '📋';
  return '📎';
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function showAttachmentError(message) {
  const toast = document.createElement('div');
  toast.className = 'attachment-error';
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}

export function clearAttachments() {
  state.attachments = [];
  renderAttachments();
}

export function getAttachmentsForApi() {
  return state.attachments.map(att => {
    // Extract base64 data from data URL (remove "data:image/png;base64," prefix)
    const base64Data = att.dataUrl.split(',')[1];
    return {
      filename: att.filename,
      media_type: att.mediaType,
      data: base64Data
    };
  });
}
