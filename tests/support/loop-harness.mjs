import { Context } from '@deepseek-ai/cordis';
import { AgentRegistry } from '@deepseek-ai/dsh-agent';
import { AgentLoop } from '@deepseek-ai/dsh-agent-loop';
import { SessionStore } from '@deepseek-ai/dsh-session';
import { SessionProjectionRegistry } from '@deepseek-ai/dsh-session-projection';
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt';
import { ToolsService } from '@deepseek-ai/dsh-tools';
import * as native from '@deepseek-ai/dsh-tool-executor-native';

// Actual published AgentLoop/AgentRegistry/Session restore. Only storage I/O and
// the provider are isolated in-memory test ports; no real DSH/Bot/model is used.
export async function loopHarness(t, { includeRuntimeContext = false } = {}) {
  const root=new Context(),stored=new Map(),requests=[];let providerCalls=0,responder=null;
  t.after(()=>root.fiber.dispose());
  const handle=entry=>({header:entry.header,inheritedEventCount:0,
    async read(){return{events:structuredClone(entry.events),eventState:'detached'};},
    async append(events){for(const e of events)if(e.seq>=entry.events.length)entry.events.push(structuredClone(e));},async close(){}});
  root.provide('sessionPersistence',{
    async create(header){const entry={header:structuredClone(header),events:[]};stored.set(header.id,entry);return handle(entry);},
    async open(id){const entry=stored.get(id);if(!entry)throw new Error('Unknown isolated Session');return handle(entry);},
  });
  root.on('session/event',(session,event)=>{const entry=stored.get(session.id);if(entry && event.seq>=entry.events.length)entry.events.push(structuredClone(event));});
  root.on('session/flush',async()=>{});
  const stream=request=>root.waterfall('llm/stream',request,()=> (async function*(){
    providerCalls++;requests.push(request);
    if(responder){yield* responder(request);return;}
    yield{type:'block-start',index:0,blockType:'text'};
    yield{type:'text-delta',index:0,text:'Isolated provider result'};
    yield{type:'block-end',index:0,block:{type:'text',text:'Isolated provider result'}};
    yield{type:'usage',usage:{inputTokens:3,outputTokens:2,cacheReadTokens:0,cacheWriteTokens:0,totalTokens:5}};
    yield{type:'finish',reason:{kind:'stop'}};
  })());
  root.provide('llm',{async prepareCall(proposal){const config=Object.freeze({...proposal,maxTokens:proposal.maxTokens??256000});return{config,adapterDefaults:proposal.maxTokens===undefined?{maxTokens:true}:{},stream};},stream});
  await root.plugin(SessionProjectionRegistry);await root.plugin(SessionStore);await root.plugin(AgentRegistry);
  await root.plugin(SystemPrompt,{includeHarnessIdentity:false,includeRuntimeContext});
  await root.plugin(ToolsService);await root.plugin(native);await root.plugin(AgentLoop);
  return{root,stored,requests,setResponse(fn){responder=fn;},get providerCalls(){return providerCalls;}};
}
