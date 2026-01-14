function t(e,r){return`${e}-${r.normalize("NFD").replace(/[\u0300-\u036f]/g,"").toLowerCase().replace(/\s+/g,"-").replace(/[()'/]/g,"")}`}export{t};
