"use strict";

// Presentation only: the server board is committed before a transient overlay
// moves its glyphs. No animation callback can submit or delay a chess move.
(() => {
  const symbols = {K:"♔",Q:"♕",R:"♖",B:"♗",N:"♘",P:"♙",k:"♚",q:"♛",r:"♜",b:"♝",n:"♞",p:"♟"};
  const records = new WeakMap(), boards = new Set();
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  const enabled = () => !reduced?.matches && !document.hidden;
  const color = (piece) => piece === piece?.toUpperCase() ? "white" : "black";
  const xy = (square) => [square.charCodeAt(0)-97,Number(square[1])-1];
  const sameBoard = (a,b) => Object.keys(a).length === Object.keys(b).length && Object.keys(a).every((key)=>a[key]===b[key]);
  const key = (state) => JSON.stringify([state.scope,state.ply,state.turn,state.lastMove,state.orientation,
    Object.keys(state.pieces || {}).sort().map((square)=>[square,state.pieces[square]])]);

  function plan(before, after) {
    if (before && after && Number.isInteger(after.ply) && before.ply === after.ply+1) {
      const forward=plan(after,before);
      return forward ? {moves:forward.moves.map((move)=>({from:move.to,to:move.from,piece:move.arrives,arrives:move.piece})),
        captures:[],restores:forward.captures,promotion:forward.promotion,reverse:true} : null;
    }
    if (!before || !after || !before.scope || before.scope !== after.scope || before.orientation !== after.orientation ||
        !Number.isInteger(before.ply) || after.ply !== before.ply+1 || before.turn === after.turn ||
        !["white","black"].includes(before.turn) || !["white","black"].includes(after.turn) ||
        !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(after.lastMove || "")) return null;
    // The live host sends an empty move list on the engine's turn. An empty
    // list means unavailable to this presentation, not an illegal engine move.
    if (Array.isArray(before.legalMoves) && before.legalMoves.length && !before.legalMoves.includes(after.lastMove)) return null;
    const from=after.lastMove.slice(0,2), to=after.lastMove.slice(2,4), promotion=after.lastMove[4];
    const piece=before.pieces[from], target=before.pieces[to], kind=piece?.toLowerCase();
    if (!piece || color(piece)!==before.turn || (target && color(target)===color(piece))) return null;
    const [fx,fy]=xy(from),[tx,ty]=xy(to),dx=tx-fx,dy=ty-fy;
    const candidate={...before.pieces}, moves=[{from,to,piece,arrives:piece}], captures=[];
    const pathClear=()=>{
      const steps=Math.max(Math.abs(dx),Math.abs(dy));
      for(let i=1;i<steps;i++)if(before.pieces[String.fromCharCode(97+fx+Math.sign(dx)*i)+(fy+Math.sign(dy)*i+1)])return false;
      return true;
    };
    if (kind==="k" && Math.abs(dx)===2 && dy===0) {
      const rank=piece==="K" ? "1" : "8", rook=piece==="K" ? "R" : "r";
      if(from!=="e"+rank || !["c"+rank,"g"+rank].includes(to) || target || promotion)return null;
      const rookFrom=(tx===6 ? "h" : "a")+rank, rookTo=(tx===6 ? "f" : "d")+rank;
      const path=tx===6 ? ["f","g"] : ["b","c","d"];
      if(before.pieces[rookFrom]!==rook || path.some((file)=>before.pieces[file+rank]))return null;
      delete candidate[rookFrom];candidate[rookTo]=rook;moves.push({from:rookFrom,to:rookTo,piece:rook,arrives:rook});
    } else if (kind==="p") {
      const direction=piece==="P" ? 1 : -1, home=piece==="P" ? 1 : 6, final=piece==="P" ? 7 : 0;
      if (dx===0) {
        if(target || !(dy===direction || (fy===home && dy===direction*2 && !before.pieces[from[0]+(fy+direction+1)])))return null;
      } else if (Math.abs(dx)===1 && dy===direction) {
        if(!target) {
          const captured=to[0]+from[1], enemy=piece==="P" ? "p" : "P";
          if(fy!==(piece==="P" ? 4 : 3) || before.pieces[captured]!==enemy)return null;
          captures.push({square:captured,piece:enemy});delete candidate[captured];
        }
      } else return null;
      if (promotion) {
        if(ty!==final)return null;
        moves[0].arrives=piece==="P" ? promotion.toUpperCase() : promotion;
      } else if(ty===final)return null;
    } else {
      if(promotion)return null;
      if(kind==="n" ? !((Math.abs(dx)===1&&Math.abs(dy)===2)||(Math.abs(dx)===2&&Math.abs(dy)===1)) :
         kind==="k" ? Math.max(Math.abs(dx),Math.abs(dy))!==1 :
         kind==="b" ? Math.abs(dx)!==Math.abs(dy) || !pathClear() :
         kind==="r" ? !((dx===0)!==(dy===0)) || !pathClear() :
         kind==="q" ? !(Math.abs(dx)===Math.abs(dy) || ((dx===0)!==(dy===0))) || !pathClear() : true) return null;
    }
    if(target)captures.push({square:to,piece:target});
    delete candidate[from];candidate[to]=moves[0].arrives;
    if(!sameBoard(candidate,after.pieces))return null;
    return {moves,captures,promotion:Boolean(promotion)};
  }

  function cleanup(record) {
    const active=record?.active;
    if(!active)return;
    record.active=null;
    for(const animation of active.animations)animation.cancel();
    for(const element of active.hidden)element.style.visibility="";
    active.layer.remove();
  }
  function cancel(board, forget=false) {
    const record=records.get(board);cleanup(record);
    if(forget && record)record.previous=null;
  }
  function cancelAll() { for(const board of boards)cancel(board,true); }
  function conceal(board,active) {
    for(const element of active.hidden)element.style.visibility="";
    active.hidden=[];
    for(const square of active.destinations) {
      const cell=[...board.children].find((item)=>(item.dataset.square || item.dataset.analysisSquare || item.dataset.coachingSquare)===square);
      const glyph=cell?.querySelector(".piece");
      if(glyph){glyph.style.visibility="hidden";active.hidden.push(glyph);}
    }
    // A grade belongs to the settled actual position, including when undoing.
    for(const cell of board.children) {
      const badge=cell.querySelector?.(".coaching-move-badge");
      if(badge){badge.style.visibility="hidden";active.hidden.push(badge);}
    }
  }
  function ghost(layer,piece,square,orientation) {
    const [file,rank]=xy(square),element=document.createElement("span");
    element.className="piece board-motion-glyph "+color(piece);
    element.textContent=symbols[piece];element.setAttribute("aria-hidden","true");
    element.style.left=((orientation==="black" ? 7-file : file)*12.5)+"%";
    element.style.top=((orientation==="black" ? rank : 7-rank)*12.5)+"%";
    layer.append(element);return element;
  }
  function start(board,record,transition,state) {
    const layer=document.createElement("div");
    layer.className="board-motion-layer";layer.setAttribute("aria-hidden","true");board.append(layer);
    const active={layer,animations:[],hidden:[],destinations:[...transition.moves.map((move)=>move.to),...(transition.restores || []).map((item)=>item.square)]};
    record.active=active;conceal(board,active);
    for(const captured of transition.captures) {
      const element=ghost(layer,captured.piece,captured.square,state.orientation);
      active.animations.push(element.animate([{opacity:1,transform:"scale(1)"},{opacity:0,transform:"scale(.78)"}],{duration:150,easing:"ease-out",fill:"forwards"}));
    }
    for(const restored of transition.restores || []) {
      const element=ghost(layer,restored.piece,restored.square,state.orientation);
      active.animations.push(element.animate([{opacity:0,transform:"scale(.8)"},{opacity:1,transform:"scale(1)"}],{duration:240,easing:"ease-out",fill:"forwards"}));
    }
    // Draw the king above its rook while their castling paths cross.
    for(const move of [...transition.moves].sort((a,b)=>Number(a.piece.toLowerCase()==="k")-Number(b.piece.toLowerCase()==="k"))) {
      const element=ghost(layer,move.piece,move.to,state.orientation),[fx,fy]=xy(move.from),[tx,ty]=xy(move.to);
      const sign=state.orientation==="black" ? -1 : 1, dx=(fx-tx)*100*sign,dy=(ty-fy)*100*sign;
      const promoting=move.piece!==move.arrives;
      active.animations.push(element.animate([
        {transform:"translate("+dx+"%,"+dy+"%) scale(.98)",opacity:1,offset:0},
        {transform:"translate(0,0) scale(1.035)",opacity:1,offset:.8},
        {transform:"translate(0,0) scale(1)",opacity:promoting ? 0 : 1,offset:1}
      ],{duration:240,easing:"cubic-bezier(.22,.7,.2,1)",fill:"forwards"}));
      if(promoting) {
        const promoted=ghost(layer,move.arrives,move.to,state.orientation);
        active.animations.push(promoted.animate([{opacity:0,transform:"scale(.9)",offset:0},{opacity:0,transform:"scale(.9)",offset:.72},{opacity:1,transform:"scale(1)",offset:1}],{duration:240,easing:"ease-out",fill:"forwards"}));
      }
    }
    Promise.all(active.animations.map((animation)=>animation.finished.catch(()=>{}))).then(()=>{if(record.active===active)cleanup(record);});
  }

  function replace(board,fragment,state) {
    let record=records.get(board);
    if(!record) {
      record={previous:null,active:null,size:null};records.set(board,record);boards.add(board);
      if(typeof ResizeObserver!=="undefined") {
        record.observer=new ResizeObserver(()=>{
          const rect=board.getBoundingClientRect(),size=[rect.width,rect.height].join(",");
          if(record.size && record.size!==size)cleanup(record);
          record.size=size;
        });record.observer.observe(board);
      }
    }
    const next={...state,pieces:{...(state.pieces || {})}}, same=record.previous && key(record.previous)===key(next);
    const canAnimate=enabled() && state.enabled!==false && typeof board.animate==="function";
    const transition=!same && canAnimate && state.animate!==false ? plan(record.previous,next) : null;
    if(!same || !canAnimate)cleanup(record);
    const layer=record.active?.layer;
    board.replaceChildren(fragment);
    if(layer){board.append(layer);conceal(board,record.active);}
    record.previous=next;
    if(transition)start(board,record,transition,next);
  }
  reduced?.addEventListener?.("change",()=>{if(reduced.matches)cancelAll();});
  window.addEventListener("resize",cancelAll);
  window.addEventListener("pagehide",cancelAll);
  document.addEventListener?.("visibilitychange",()=>{if(document.hidden)cancelAll();});
  window.RegicideMotion={replace,cancel,plan};
})();
