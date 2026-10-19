# Schema del vault

Questo vault è la memoria di un brain: note tue, pagine scritte dall'agente, fonti grezze e risposte salvate. Le cartelle qui sotto sono le zone del vault. Il codice ragiona sempre sui nomi logici (`inbox`, `raw`, `calls`, ...), e il blocco in fondo a questa pagina dice quale cartella è ogni zona in questa lingua. Se rinomini una cartella, cambia anche la riga corrispondente qui: nient'altro nel codice va toccato.

`raw/` contiene parole di altri: trascritti, mail, pagine web, file inoltrati. È immutabile dopo l'ingresso e vale come non fidato, quindi una pagina derivata da `raw/` porta `trust: untrusted`. Tutto il resto è fidato finché non lo si marca diversamente.

Il frontmatter è sempre in inglese, anche quando il vault è in italiano: `type`, `created`, `updated`, `tags`, `domain`, `share`, `sources`, `confidence`, `contested`, `trust`, `status`, `date`, `start`, `participants`, `organizer`, `event`, `audio`, `transcript`, `category`, `parties`, `issued`, `expires`, `original`, `original_sha256`, `project`, `jira`, `due`, `index`. Cartelle, nomi dei file e testo seguono invece la lingua del vault.

`types` e `tags` sono tassonomie chiuse: se serve un valore nuovo si aggiunge qui, con una modifica che passa da una conferma. Le pagine in `archivio/` portano `index: false` e restano fuori dal richiamo automatico e dal lint.

Le pagine che l'agente tiene su di sé in `_brain/` sono nominate in `brain_pages`: il codice e le skill usano i nomi logici (`commitments`, `initiative`, ...), quindi rinominare una pagina è una modifica solo qui.

```yaml
language: it
zones:
  inbox: inbox
  raw: raw
  calls: call
  topics: argomenti
  knowledge: conoscenza
  ideas: idee
  projects: progetti
  people: persone
  work: lavoro
  documents: documenti
  answers: risposte
  drawings: disegni
  archive: archivio
  _brain: _brain
  _meta: _meta
types:
  capture: cattura da elaborare, ancora in inbox
  call: nota di una call, con il link al trascritto
  transcript: trascritto completo di una call
  topic: scheda per argomento, aggiornata dalle call
  knowledge: pagina di conoscenza su un dominio
  idea: idea, con il suo stato
  project: progetto attivo
  listing: annuncio o offerta raccolti durante una ricerca
  document: scheda di un documento, con il codice dell'originale
  daily: nota della giornata
  task: attività, con il riferimento a Jira se c'è
  person: una pagina per persona ricorrente
  answer: risposta buona salvata
  drawing: disegno Excalidraw
  brain: pagina di memoria dell'agente
tags:
  - lavoro
  - personale
  - casa
  - salute
  - soldi
  - tecnologia
  - studio
  - viaggi
  - sport
  - persone
untrusted_zones:
  - raw
brain_pages:
  commitments: impegni
  initiative: iniziativa
  policy: policy
  positions: posizioni
  preferences: preferenze
  profile: profilo
  lint: lint
```
