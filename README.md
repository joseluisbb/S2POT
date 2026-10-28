# S2POT & S2POTlite - OCR & HTR Tool

Ferramenta local e offline de processamento de documentos, conversão de formatos, compressão e reconhecimento de texto:
- **OCR (Optical Character Recognition)** para texto impresso (RapidOCR / PaddleOCR ONNX).
- **HTR (Handwritten Text Recognition)** para caligrafia cursiva contínua (Microsoft TrOCR Small INT8 via ONNX Runtime).

---

## 🚀 Aplicações

1. **S2POT** (`main.py` / `S2POT.exe`):
   - Processamento individual ou em lote (batch).
   - Conversão e exportação para PDF pesquisável, Word (`.docx`), e Texto (`.txt`).
   - Opções de compressão de imagem (DPI, qualidade JPEG/WebP).
   - Deteção e seleção de modo: Texto Impresso ou Caligrafia Cursiva (TrOCR).

2. **S2POTlite** (`main_lite.py` / `S2POTlite.exe`):
   - Interface rápida e compacta com suporte a *Arrastar & Largar* (Drag & Drop).
   - Processamento imediato com relatório condensado.

---

## 🛠️ Instalação e Execução

### Requisitos
- Python 3.10+
- Dependências listadas em `requirements.txt`

```bash
pip install -r requirements.txt
```

### Executar a partir do código-fonte:
```bash
# Aplicação completa
python main.py

# Versão Lite
python main_lite.py
```

### Compilação unificada (PyInstaller):
Gera `dist/S2POT/` contendo ambos os executáveis (`S2POT.exe` e `S2POTlite.exe`) partilhando a mesma pasta `_internal`:
```bash
pyinstaller S2POT.spec
```
