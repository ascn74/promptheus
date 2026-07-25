# Promptheus

> *Prometeu roubou o fogo dos deuses e o entregou aos humanos.*

Aplicação web que recebe um prompt e um ou mais arquivos, envia para os modelos
selecionados no [OpenRouter](https://openrouter.ai) e apresenta as respostas
lado a lado para comparação.

## Estado

**Em definição.** Este repositório contém apenas a base do projeto — a
arquitetura e os requisitos ainda estão sendo desenhados. Nenhum código de
aplicação foi escrito.

## Escopo previsto

- Um prompt, N modelos: envio em paralelo e comparação das respostas
- Seleção de modelos a partir do catálogo do OpenRouter
- Anexos: texto e código, imagens (para modelos multimodais), PDF e DOCX
- Backend em Python; a chave da API fica no servidor, nunca no navegador

## Requisitos

- Python 3.12+
- Uma chave de API do OpenRouter

## Configuração

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env   # preencha OPENROUTER_API_KEY
```

## Licença

MIT — veja [LICENSE](LICENSE).
