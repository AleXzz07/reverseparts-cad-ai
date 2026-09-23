# ReverseParts CAD AI — Viewer 3D V1.1

## Provenienza e ambito

Base: `reverseparts-cad-ai-viewer-v1-profiler-fix.zip`. La copia congelata
`scripts/_viewer_v1_baseline.py` coincide byte per byte con
`app/model_exporter.py` dello ZIP iniziale (SHA-256
`6b2b967204e73bda73dd450690456e1ed9293f893a9fca95bc01db76ecdacc90`).
Serve al profiler per eseguire la V1 originale senza ricostruirla a posteriori.

Sono stati modificati `app/model_exporter.py` e
`scripts/profile_viewer_v1.py`; aggiunti la copia V1 e
`tests/test_viewer_v1_1.py`. Nessuna cache o modifica Phase 4B.

## Modifiche V1.1

- Per una `Plane`/`GeomPlane`, la normale analitica OCC è interrogata una
  volta sulla prima posizione della faccia. L'allineamento al winding e la
  normale mesh di fallback restano locali a ciascun vertice della faccia.
  Se l'interrogazione iniziale fallisce, tutta la faccia usa il fallback.
- Per le superfici curve, incluse le B-spline, restano `parameter` e
  `normalAt` OCC per ogni vertice, con il fallback originale.
- Una lista di vertici già prodotta dalla tessellazione non viene copiata.
  Buffer di posizioni, normali, indici e linee vengono scritti con
  `struct.pack_into` in buffer dimensionati una volta, preservando ordine,
  formato e byte del GLB nei test sintetici.
- Il profiler esegue legacy, V1 congelata e V1.1 sugli stessi tre STEP in
  nove processi separati. Riporta caricamento STEP, tessellazione, calcolo
  normali, elaborazione bordi B-Rep, assemblaggio GLB, generazione totale,
  durata totale del worker, triangoli, vertici, segmenti B-Rep, dimensione
  GLB e picco RSS del worker. Salva JSON e CSV.
- Per ogni coppia V1/V1.1 calcola SHA-256 dei buffer POSITION, NORMAL,
  indici, bordi e dell'intero GLB. Il profiler restituisce codice non zero
  se un export fallisce, se un conteggio differisce o se un buffer differisce.
  Legacy è una geometria differente e non è soggetta a equivalenza byte.

Il campo `occ_normals_sec` cronometra tutta la routine delle normali di faccia:
interrogazioni OCC, calcolo del fallback e allineamento. La separazione dalla
tessellazione avviene con un wrapper diagnostico identico nei due percorsi.
Per legacy il campo è `null`: le normali mesh sono calcolate durante
`glb_assembly_sec`. `model_generation_sec` comprende anche la scrittura degli
snapshot e gli hash, quindi non è necessariamente la somma dei tempi delle
fasi. `total_sec` parte dall'ingresso nel worker e comprende import FreeCAD e
STEP, ma non l'avvio dell'interprete prima dell'ingresso nel worker.

## Verifiche eseguite in questo ambiente

- Regressioni mirate: `16 passed, 1 skipped` dopo l'aggiunta del controllo
  della copia congelata. Il test saltato richiede FreeCAD.
- Suite `python -m pytest tests -q`: `257 passed, 57 skipped, 2 warnings`.
- `python -m compileall` dei moduli viewer e profiler: superato.
- `python scripts/profile_viewer_v1.py --help`: opzioni legacy, v1, v1_1
  disponibili.
- SHA-256 contro lo ZIP iniziale: invariati `app/cad_analyzer.py`,
  `app/sheetmetal_unfolder.py`, `app/quote_engine.py`, `app/pdf_report.py`,
  `app/preview_renderer.py`, `app/preview_service.py`, `frontend/index.html`
  e `public/index.html`.

FreeCAD e Docker non sono installati qui. I nove export STEP reali, il
benchmark SM06–SM13, la suite Docker completa, l'equivalenza sui STEP reali e
il picco RAM nel container **non sono stati misurati**. I test sintetici
verificano i buffer GLB, ma non dimostrano equivalenza o miglioramento sui
tre pezzi reali. Una parametrizzazione OCC che fallisca solo per alcuni
vertici di una faccia planare potrebbe produrre fallback diversi rispetto
alla V1: il confronto degli hash segnalerà questa eventualità.

## Verifica sul PC — PowerShell con Docker Desktop

Eseguire dalla cartella estratta dello ZIP, con Docker avviato:

```powershell
docker build -t reverseparts-viewer-v1-1 .
docker run --rm reverseparts-viewer-v1-1 python3 -m pytest tests -q
docker run --rm reverseparts-viewer-v1-1 python3 scripts/run_sheetmetal_benchmark.py
```

Il benchmark stampa i risultati SM06–SM13: controllare che tutti gli otto
`passed` siano `true`. Il comando restituisce un errore se un caso fallisce.

```powershell
New-Item -ItemType Directory -Force .\viewer-v1-1-profile | Out-Null
docker run --rm --memory=1g --memory-swap=1g `
  --mount "type=bind,source=$($PWD.Path)\viewer-v1-1-profile,target=/output" `
  reverseparts-viewer-v1-1 `
  python3 scripts/profile_viewer_v1.py --output-dir /output --timeout-sec 180
```

Aprire `viewer-v1-1-profile/viewer_v1_1_profile.json` e
`viewer_v1_1_profile.csv`. Verificare nove righe con `status=completed`,
`v1_vs_v1_1_equivalence` con tre `status=pass`, le misure delle fasi e
`peak_worker_rss_mib`. Il limite 1 GiB è per la raccolta iniziale; per il
gate da 512 MiB ripetere con `--memory=512m --memory-swap=512m` e
`--output-dir /output/limit-512m`. Un worker terminato da OOM conserva la
fase raggiunta, exit code e segnale nel JSON.

Il profiler misura soltanto il lavoro server per STEP→GLB. Caricamento via
rete e parsing/rendering Three.js nel browser richiedono una verifica visiva
separata. Nessun miglioramento prestazionale V1.1 è dichiarato prima delle
misure Docker sugli STEP reali.
