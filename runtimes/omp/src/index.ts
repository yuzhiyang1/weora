import { serve } from '../../pi/src/protocol.js';
import { runOmp } from './runtime.js';

await serve(runOmp);
