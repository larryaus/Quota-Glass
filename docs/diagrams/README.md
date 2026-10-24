# Architecture diagrams

The README embeds these as PNGs because the GitHub mobile app does not render
Mermaid. The `.mmd` files are the source; after editing one, regenerate both
variants from the repository root:

```bash
npx -p @mermaid-js/mermaid-cli mmdc -s 2 -b white -i docs/diagrams/NAME.mmd -o docs/diagrams/NAME.png
npx -p @mermaid-js/mermaid-cli mmdc -s 2 -t dark -b '#0d1117' -i docs/diagrams/NAME.mmd -o docs/diagrams/NAME-dark.png
```

Keep each diagram narrow (under about 480px before scaling) so it stays
legible on a phone.
