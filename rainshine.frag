#version 300 es
precision mediump float;

// Rainbow Rain Shader — 10x30 pixel grid

uniform float uTime;      // drive with absTime.seconds
uniform float uSpeed;     // default ~4.0 (pixels per second)
uniform int   uTrailLen;  // default 10 (pixels)
uniform float uDensity;   // default 3.0 (>=1: drops per column, <1: probability of a drop)

out vec4 fragColor;

// --- helpers ---------------------------------------------------------------
float hash(float n) { return fract(sin(n * 127.1) * 43758.5453123); }
float hash2(float a, float b) { return fract(sin(a * 127.1 + b * 311.7) * 43758.5453123); }

vec3 hsv2rgb(float h, float s, float v) {
    vec3 k = mod(vec3(h * 6.0, h * 6.0 - 2.0, h * 6.0 - 4.0), 6.0);
    return v * mix(vec3(1.0), clamp(2.0 - abs(k - 3.0), 0.0, 1.0), s);
}

void main()
{
    // Grid constants
    const int COLS = 10;
    const int ROWS = 30;

    float speed    = uSpeed > 0.0 ? uSpeed : 4.0;
    int   trailLen = uTrailLen > 0 ? uTrailLen : 10;
    float density  = uDensity  > 0.0 ? uDensity  : 3.0;

    // When density < 1, it's a probability each column has 1 drop
    // When density >= 1, it's the number of drops per column
    int numDrops = max(int(ceil(density)), 1);

    // Current pixel coordinate (integer) — use gl_FragCoord for exact pixel
    int col = int(gl_FragCoord.x);
    int row = int(gl_FragCoord.y);
    col = clamp(col, 0, COLS - 1);
    row = clamp(row, 0, ROWS - 1);

    vec3 color = vec3(0.0);

    // Each column has multiple rain drops staggered in time
    for (int d = 0; d < numDrops; ++d) {
        // Better seeding: use hash2 with column and drop index for decorrelation
        float seed = hash2(float(col) + 0.5, float(d) + 0.5) * 1000.0;

        float rate = 0.5 + 0.5 * hash(seed + 1.0);          // speed variation
        float phase = hash(seed + 2.0) * float(ROWS + trailLen);

        // Head position in row-space, falling downward (top=29 to bottom=0)
        float cycle = float(ROWS + trailLen);
        float headF = mod(phase + uTime * speed * rate, cycle);

        // For density < 1, use time-varying probability per cycle
        // Each time a drop wraps around, re-roll whether it's visible
        if (density < 1.0) {
            float cycleIndex = floor((phase + uTime * speed * rate) / cycle);
            float roll = hash2(seed + 3.0, cycleIndex);
            if (roll > density) continue;
        }
        int   headRow = int(floor(headF));

        // Head falls from row 29 down to row 0
        // Trail extends ABOVE the head (higher row numbers)
        int dist = row - (ROWS - 1 - headRow);
        if (dist < 0) dist += ROWS + trailLen;  // wrap

        if (dist == 0) {
            // White head pixel
            color += vec3(1.0);
        } else if (dist > 0 && dist <= trailLen) {
            // Rainbow trail: hue shifts along trail, offset per drop
            // Luminance decreases further from head
            float t = float(dist) / float(trailLen);
            float hue = fract(t + hash(seed));
            float saturation = 0.7 + 0.3 * (1.0 - t);  // slightly desaturate far end
            float brightness = 1.0 - t * t;             // quadratic falloff for luminance
            color += hsv2rgb(hue, saturation, brightness);
        }
    }

    color = clamp(color, 0.0, 1.0);
    fragColor = vec4(color, 1.0);
}
