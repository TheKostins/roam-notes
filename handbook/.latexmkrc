# build engine
$recorder = 1;
$pdflatex = 'TEXMFVAR=build/texmf-var TEXMFCACHE=build/texmf-var lualatex -interaction=nonstopmode -synctex=1 -recorder %O %S';
$pdf_mode = 1;

# put build artifacts into build/ inside the course directory (because we use -outdir=build)
