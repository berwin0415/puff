# Video Board Rendering Design

## 1. Module Scope

The video board rendering module is the low-level preview engine of the video editor. It receives a rendering schema from the editor layer, turns tracks and elements into runtime objects, and renders the active elements into a Pixi-based canvas.

The module does not own timeline editing logic such as snapping, track insertion, drag sorting, or clip trimming UI. Those behaviors live in the editor/timeline layer. The rendering module focuses on:

- Pixi application initialization
- Track and element rendering
- Resource loading
- Play, pause, stop, and seek
- Active element mounting and unmounting
- Canvas coordinate mapping
- Basic canvas interaction events

The high-level boundary is:

```text
Editor Layer
  owns: timeline editing, draft conversion, element config, business state

Rendering Module
  owns: schema rendering, media playback, Pixi stage updates, resource lifecycle

Runtime Layer
  owns: Pixi WebGL rendering, HTMLVideoElement, HTMLAudioElement
```

## 2. Core Design Idea

The module follows a mixed model:

- Declarative input: the editor passes a schema describing tracks and elements.
- Imperative rendering: the renderer creates and manages Pixi display objects and native media elements.

The key principles are:

- Data drives rendering.
- Time determines visibility.
- Each element type owns its own media behavior.
- The stage only mounts elements active at the current playback time.

## 3. Core Entities

### VideoBoard

`VideoBoard` is the facade and coordinator of the rendering module.

Responsibilities:

- Initialize the Pixi application.
- Hold global playback state.
- Receive schema updates.
- Drive the ticker loop.
- Provide play, pause, stop, and seek commands.
- Emit lifecycle and interaction events.
- Install rendering plugins.

Conceptually, it is the root controller of the canvas.

### ContentManager

`ContentManager` manages the Pixi stage content.

Responsibilities:

- Own the render container.
- Own the video-area background.
- Convert business coordinates into render coordinates.
- Mount active element display objects.
- Unmount inactive element display objects.
- Apply element layout.
- Sort display object layers.
- Maintain the current set of mounted elements.

It answers the question: what Pixi objects are currently visible on the canvas?

### BoardTrackManager

`BoardTrackManager` manages all render tracks.

Responsibilities:

- Create, reuse, and remove tracks based on schema.
- Sort tracks by layer.
- Query elements active at a given playback time.
- Find elements by id.

It does not represent editor timeline UI tracks. It represents render-time tracks.

### BoardTrack

`BoardTrack` manages the elements inside one render track.

Responsibilities:

- Create or reuse element instances.
- Sort elements by their entrance time.
- Pass track-level display and mute state to elements.
- Maintain previous and next element references.

### BaseBoardElement

`BaseBoardElement` is the shared abstraction for renderable elements.

Responsibilities:

- Store common schema fields.
- Track load and play state.
- Calculate element-local playback time.
- Apply common layout.
- Expose lifecycle methods such as load, play, pause, stop, seek, and dispose.

Concrete element classes extend it.

### Concrete Elements

The renderer supports several element types:

- Video element
- Image element
- Text element
- Audio element
- Transition element
- Chart element

Each element type owns its own resource and playback implementation.

Examples:

```text
Video Element
  Pixi Sprite + HTMLVideoElement

Image Element
  Pixi Sprite + Texture

Audio Element
  HTMLAudioElement + invisible Sprite placeholder

Text Element
  Pixi Text / Canvas Texture / Shader / GSAP animation

Transition Element
  Shader / Mesh / effect texture
```

## 4. Core Data Model

The renderer consumes a three-level schema:

```text
VideoBoardOptions
  └─ Track[]
      └─ Element[]
```

### VideoBoardOptions

Represents the global render state.

Main fields:

- Current playback time
- Total duration
- Track list
- Resize mode
- Interval type
- Debug config
- Loading config

### Track

Represents one render track.

Main fields:

- Track id
- Layer
- Display state
- Mute state
- Element list

The track layer controls cross-track render order.

### Element

Represents one renderable media item or effect.

Main fields:

- Element id
- Element type
- Resource url
- Entrance time
- Leave time
- Source begin time
- Source end time
- Position
- Size
- Rotation
- Crop information
- Volume
- Animation or effect config

Element data contains two categories of information:

```text
Temporal data
  decides when the element appears and what source time it should play.

Spatial data
  decides where and how the element appears on the canvas.
```

## 5. Runtime Rendering Flow

### Initialization

```text
Create VideoBoard
  -> Create Pixi Application
  -> Create Pixi Ticker
  -> Create BoardTrackManager
  -> Create ContentManager
  -> Add background layer
  -> Add render container layer
  -> Install plugins
  -> Initialize Pixi renderer
  -> Append canvas to host DOM container
```

After initialization, the board is ready to receive schema updates.

### Schema Update

```text
External schema arrives
  -> VideoBoard merges config
  -> VideoBoard emits optionUpdate
  -> UpdatePlugin receives optionUpdate
  -> BoardTrackManager updates tracks
  -> BoardTrack updates elements
  -> Renderer queries active elements at current time
  -> ContentManager updates Pixi container
```

The update process tries to reuse existing objects.

```text
Same track id
  -> reuse track instance

Same element identity
  -> reuse element instance

New track or element
  -> create instance

Missing track or element
  -> unmount or dispose as needed
```

Reuse is important because media and Pixi resources are expensive to recreate.

### Playback Tick

When playing, Pixi ticker advances the board time.

```text
Ticker tick
  -> Calculate next playback time
  -> Preload elements in the near future
  -> Query elements active at next time
  -> Update Pixi stage elements
  -> Commit currentTime
  -> Emit tick
```

Element activation is time-window based:

```text
element.in <= currentTime < element.out
```

Only active elements are mounted into the render container.

### Seek

Seek is a forced time jump.

```text
seek(targetTime)
  -> Set currentTime
  -> Query elements active at targetTime
  -> Load active element resources
  -> Seek each active element to its local time
  -> Update Pixi stage
  -> Emit afterSeek
```

Element-local time is calculated as:

```text
elementLocalTime = boardCurrentTime - elementInTime + sourceBeginTime
```

Example:

```text
Element enters at board time 10s.
Element source begins from 3s.
Board current time is 15s.

Element local playback time = 15s - 10s + 3s = 8s.
```

## 6. Canvas Coordinate System

The rendering module uses two coordinate systems.

### Business Coordinates

These correspond to final video resolution.

Example:

```text
1920 x 1080
```

Editor draft data and export data usually use this coordinate space.

### Render Coordinates

These correspond to the actual preview canvas size.

Example:

```text
960 x 540
```

The user sees this coordinate space in preview.

### Coordinate Mapping

`ContentManager` converts between these two spaces.

```text
Business coordinates -> Render coordinates
Render coordinates -> Business coordinates
```

This allows the preview canvas to scale independently from the final video output.

It also allows drag, resize, and rotate interactions to be converted back into stable business data.

## 7. Stage Structure

The Pixi display tree is conceptually:

```text
Pixi Application
  └─ Root Stage
      ├─ Background
      └─ Render Container
          ├─ Active Element A
          ├─ Active Element B
          ├─ Active Element C
          └─ Active Transition Element
```

The background is separated from the render container.

The render container holds currently active elements only.

Layer order is controlled by:

- Track layer
- Element order in active element list
- Special transition element rules

## 8. Element Lifecycle

An element generally moves through this lifecycle:

```text
Create
  -> Load resource
  -> Mount to stage
  -> Layout
  -> Seek to local time
  -> Play or pause
  -> Unmount from stage
  -> Dispose
```

### Create

`BoardTrack` creates the element based on its type through the element registry.

### Load

Each element class loads its own resource.

```text
Video
  loads a video texture and controls an HTMLVideoElement.

Image
  loads an image texture.

Audio
  creates and controls an HTMLAudioElement.

Text
  creates Pixi text, canvas texture, shader texture, or animation container.

Transition
  creates shader or mesh resources.
```

### Mount

Only elements active at the current playback time are mounted into the Pixi render container.

### Layout

Common layout is handled by the base element abstraction:

```text
Convert business location to render location
Set center position
Set pivot or anchor
Set rotation
Set render size
```

### Playback

Playback commands are distributed from `VideoBoard` to mounted elements.

### Unmount

When an element leaves the current time window, it is removed from the Pixi render container.

The element instance may still remain in the track model for reuse.

### Dispose

When the track or board is destroyed, elements and Pixi resources should be disposed.

## 9. Resource Loading Model

The renderer uses a lazy and near-future preload strategy.

Current active elements must be loaded before seek completes.

Future elements are preloaded shortly before they appear.

The current strategy can be described as:

```text
On playback tick
  -> Load resources needed within the next few seconds
  -> Mount only current active elements

On seek
  -> Load target-time active elements immediately
  -> Seek them before stage update completes
```

This balances responsiveness and resource cost.

## 10. Event Model

The renderer uses events to connect core logic, plugins, and external consumers.

Important events:

```text
optionUpdate
  schema or config changed

tick
  playback time advanced

beforeSeek
  seek is about to start

afterSeek
  seek has completed

waitting
  renderer is waiting for resources

canplay
  resources are ready enough to play

mounted
  element mounted to stage

unmount
  element removed from stage

focus
  element focused

blur
  focus cleared

elementChanged
  element position or size changed on canvas

resize
  canvas dimensions changed

destroy
  renderer is being destroyed
```

Plugins subscribe to these events to extend board behavior.

## 11. Plugin Model

`VideoBoard` installs plugins to keep the core object smaller.

Current plugin roles:

```text
UpdatePlugin
  Handles schema updates and refreshes active elements.

ResizePlugin
  Handles canvas resize.

MouseEventPlugin
  Handles pointer and click events.

FocusPlugin
  Handles element focus state.

DragPlugin
  Handles canvas-level drag behavior.

LoadingPlugin
  Handles loading and canplay state.
```

The plugin model makes rendering behaviors extensible without putting all logic in `VideoBoard`.

## 12. Interaction With Editor Layer

The editor layer owns its own track and element models.

Before rendering, the editor converts its business model into a board schema:

```text
Editor track model
  -> Render track schema

Editor element model
  -> Render element schema
```

The board consumes only the render schema.

When canvas interaction changes an element, the board emits changed element data.

The editor layer then converts the changed render coordinates back into business coordinates and updates its own model.

This creates a two-way bridge:

```text
Editor model -> Render schema -> Pixi canvas
Pixi interaction -> Changed element schema -> Editor model
```

## 13. Current Design Strengths

### Clear Runtime Layers

The module has understandable layers:

```text
VideoBoard
BoardTrackManager
BoardTrack
ContentManager
Element classes
Plugins
```

Each layer has a mostly clear responsibility.

### Schema-Driven Rendering

The editor does not need to know Pixi details.

This keeps the editor business layer decoupled from the rendering implementation.

### Element Extensibility

New element types can be introduced by:

```text
Create a new element class
Register it with the element registry
Emit matching element type in schema
```

### Playback and Editing Share One Data Model

Preview, seek, element focus, and canvas interaction all revolve around the same schema model.

This reduces duplicate conversion paths.

## 14. Current Design Risks

### Async Resource Loading Can Race With Stage State

Resource loading is asynchronous, but stage updates are mostly synchronous diff operations.

Risk pattern:

```text
Element starts loading
  -> Time or schema changes
  -> Element is no longer active
  -> Old load finishes
  -> Old element attempts to mount
```

This can cause flicker, stale nodes, or inconsistent mounted element state.

### Schema Update and Resource Preload Are Not a Single Transaction

Schema updates can trigger track updates before all relevant resources are ready.

Risk pattern:

```text
New schema arrives
  -> Track update starts
  -> Resource preload starts
  -> Another schema arrives
  -> Old resource result finishes late
```

Without version checks, stale async results can affect current render state.

### Element Identity Must Be Stable

Element reuse depends on stable id and type.

If the editor layer regenerates ids frequently, the renderer will recreate elements unnecessarily.

This can harm performance and playback stability.

### Media Elements and Pixi Objects Have Different Lifecycles

Video and audio rely on native media elements.

Images, text, charts, and transitions rely mostly on Pixi display objects and textures.

The lifecycle abstraction is shared, but actual behavior differs.

This can cause subtle inconsistencies in load, seek, pause, stop, and dispose.

## 15. Recommended Abstractions

### RenderTransaction

Introduce a render transaction for every schema or time update.

```text
RenderTransaction
  ├─ version
  ├─ targetTime
  ├─ nextTracks
  ├─ nextActiveElements
  ├─ resourceTasks
  └─ commit
```

Only the latest transaction should be allowed to commit to the Pixi stage.

### ResourceManager

Extract resource responsibilities.

```text
ResourceManager
  owns:
    loading
    caching
    cancellation
    stale result protection
    media source reuse
```

### StageManager

Make stage mutation explicit.

```text
StageManager
  owns:
    mount
    unmount
    sort
    layout
    cleanup
```

This would separate resource readiness from display object mutation.

### Element State Machine

Give each element a more explicit lifecycle.

```text
unloaded
loading
ready
mounted
playing
paused
error
disposed
```

This would make cross-element behavior easier to reason about.

### Schema Diff Policy

Make schema diff rules explicit.

```text
Same id and same type
  reuse element instance

Same resource
  reuse loaded resource

Only layout changed
  update layout only

Only time changed
  seek or visibility update only

Type changed
  dispose and recreate
```

## 16. Summary

The video board rendering module is best understood as a time-window-based Pixi playback engine.

It receives a track schema, creates render tracks and render elements, and mounts only the elements active at the current playback time. Media elements are controlled through native browser media APIs, while visual elements are rendered through Pixi textures, containers, shaders, and animation timelines.

The current architecture has a solid separation of concerns and a useful schema-driven model. The main area to improve is consistency around asynchronous resource loading and stage mutation. A transaction-based update model and clearer resource/stage separation would make the renderer more robust under rapid seek, frequent editing, and high-frequency schema updates.
