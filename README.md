# Ar-condicionado LG ThinQ (Windows)

Painel flutuante para controlar o ar-condicionado LG pela nuvem LG ThinQ, usando a biblioteca
`wideq` do projeto [ha-smartthinq-sensors](https://github.com/ollo69/ha-smartthinq-sensors) v0.43.0
(copiada em `wideq/`; o release completo está em `vendor/`).

## Como usar

- Abra pelo atalho **Ar-condicionado LG** na Área de Trabalho (ou `pythonw app.py`).
- Na primeira vez, entre com a conta LG ThinQ (a mesma do celular).
- O app fica no ícone da bandeja do sistema: clique nele para abrir o painel.

## Arquivos

- `app.py`: janela, ícone da bandeja e ponte com a interface
- `lg_backend.py`: conexão com a LG ThinQ e comandos do aparelho
- `ui/index.html`: interface
- Configuração e log: `%USERPROFILE%\.arcondicionado-lg\`

Dependências: `pip install -r requirements.txt`
