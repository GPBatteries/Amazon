# Amazon Daily

Haalt dagelijks de salesdata uit de Google Sheet **Daily SS** (tab `Daily IMP`) en bouwt
daar een Excel-rapport van per ASIN, opgezet volgens de spec in tab `DB Voorbeeld`.

Draait volledig in GitHub Actions (geen pc nodig die aan staat). Het resultaat komt in
`output/` en wordt teruggecommit naar de repo. Je downloadt het bestand via de
GitHub Pages-pagina, of haalt het lokaal binnen met `git pull`.

## Bestanden

| Bestand | Plek |
|---------|------|
| `update_amazon.py` | root |
| `requirements.txt` | root |
| `index.html` | root (de downloadpagina) |
| `daily.yml` | `.github/workflows/` |
| `output/` | wordt vanzelf gevuld |

## Hoe het werkt

1. De Action draait elke dag om **11:30 UTC** (12:30 winter / 13:30 zomer NL), dus na de
   update van de sheet om 12:00.
2. `update_amazon.py` leest `Daily IMP` als CSV-export (de sheet staat op "iedereen met
   de link kan bekijken", dus er is geen inlog of service-account nodig).
3. Per datum staan in `Daily IMP` dubbele rijen (een blok zonder en een blok met traffic).
   Het script dedupliceert naar 1 rij per datum: units en sales zijn in beide blokken
   gelijk, voor CVR wordt automatisch de rij met traffic gepakt.
4. Het bouwt `output/Amazon_<ASIN>.xlsx` opnieuw uit de volledige historie. Elke dag komt
   er dus vanzelf een nieuwe datumkolom bij.

## De Excel

Twee tabs:

- **Data** (verborgen): de ruwe, gededupliceerde bron waar de formules uit lezen.
- **Rapport**: de tabel volgens `DB Voorbeeld`. Eén datumkolom per dag.

Alleen **Sales #** en **Sales £** komen uit de sheet (via `SUMIFS`). De rest volgt daaruit:

| Rij | Formule |
|-----|---------|
| Avg. RSP (gross) | Sales £ / Sales # |
| Avg. RSP (net) | gross / (1 + BTW) |
| Commission + AdFee | 15,3% van gross RSP |
| FBA / COGS | vaste kostprijs per stuk (aanname) |
| Margin (abs) | net − commission − FBA − COGS |
| Margin (%) | margin abs / net |
| Margin Tot. (abs) | margin abs × units |
| Margin Net. (abs) | bewust leeg |
| CVR | direct uit `Daily IMP` kolom AF |

De **aannames** (BTW, commissie %, FBA, COGS) staan als bewerkbare blauwe cellen onderaan
de Rapport-tab. Pas je die in Excel aan, dan herrekent alles mee.

> De gecommitte xlsx bevat formules, geen voorberekende waarden. Excel rekent ze direct
> door zodra je het bestand opent.

## Eenmalige setup

1. Zet deze bestanden in de repo `Amazon` en push naar GitHub.
2. Ga naar tab **Actions** en zet workflows aan als GitHub daarom vraagt.
3. Zet **Settings -> Pages** op deploy from branch `main` / root.
4. Controleer dat de Google Sheet op **"iedereen met de link kan bekijken"** staat.
5. Test met **Run workflow** (knop bij "Daily Amazon update"). Daarna staat het bestand in
   `output/` en is de downloadpagina live.

## Aanpassen

- **Andere aannames**: in `update_amazon.py` bovenaan, of direct in de blauwe cellen in Excel.
- **Andere/extra ASIN**: pas `ASIN` aan in `update_amazon.py`.
- **Ander tijdstip**: pas de `cron` in `.github/workflows/daily.yml` aan (in UTC).

## Lokaal draaien (optioneel)

```bash
pip install -r requirements.txt
python update_amazon.py
```

## Downloadpagina (GitHub Pages)

Na setup staat er een downloadknop op:

```
https://pietergp25.github.io/Amazon/
```

De pagina toont wanneer het bestand voor het laatst is bijgewerkt en welke datum als
laatste in de data zit, met een knop om de actuele Excel te downloaden. Geen lokale
opslag of `git pull` meer nodig.

Pages aanzetten: **Settings -> Pages -> Source: Deploy from a branch -> `main` / root**.
Bij elke dagelijkse commit wordt de pagina opnieuw uitgerold met de nieuwste cijfers.

## Automatisch ophalen op je pc (optioneel)

Liever lokaal in plaats van via de pagina? Kloon de repo eenmalig en laat Windows
Taakplanner dagelijks een pull doen, bijvoorbeeld rond 12:35:

```
git -C "C:\pad\naar\Amazon" pull
```

Daarna open je `output\Amazon_B00CO00Y32.xlsx` lokaal.
